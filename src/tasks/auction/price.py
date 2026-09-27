"""拍卖价格纯逻辑: OCR 数值解析, 每轮指定价格, 加价计算, 估价换算, 按键序列。

本模块全部是无 IO 的纯函数: 输入配置值与原始 OCR 读数, 输出解析/计算结果,
不依赖任务实例、不读配置、不发日志。配置读取、OCR 副作用、告警与回退基础价
仍由 AutoBidAuctionTask 协调; 新增价格规则优先在这里表达, 便于直接单测。
"""

import re
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from src.tasks.auction.layout import FULLWIDTH_NUMERIC, PAD_SHORTCUTS
from src.tasks.auction.options import CONF_BID_PRICES, RAISE_MODE_MULTIPLE, RAISE_MODE_PERCENT


def parse_asset_value(raw_text: str) -> int | None:
    """统一解析资产 OCR 文本, 返回整数或 None。

    处理流程:
    1. 全角数字与全角逗号转半角.
    2. 修正常见 OCR 错误 (O -> 0, l/I -> 1).
    3. 提取数字.
    4. 转换为 int, 失败时返回 None.
    """
    normalized = raw_text.translate(FULLWIDTH_NUMERIC)
    corrected = normalized.replace("l", "1").replace("I", "1").replace("O", "0")
    digits = re.sub(r"[^\d]", "", corrected)
    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def is_partial_number_text(raw_text: str) -> bool:
    """判断 OCR 文本是否为「首位数字被漏读」的残缺读数。

    估价按千位分隔显示, 逗号前面必须有数字。首位数字在区域最左侧, OCR 对它的识别
    不稳定, 漏读时会剩下 ",544" 这种逗号前空着的文本 (线上日志 18:56 那局连着 9 次),
    它对应的真实值至少是 "x,544"。把它当结果会按低一个数量级的价格出价。
    """
    normalized = raw_text.translate(FULLWIDTH_NUMERIC)
    digits_and_commas = re.sub(r"[^\d,]", "", normalized)
    return digits_and_commas.startswith(",")


def has_inconsistent_grouping(raw_text: str) -> bool:
    """判断带千位分隔符的读数是否「位数与逗号不自洽」。

    千位分隔的合法形式只有 `1,234` / `12,345` / `123,456` 这几种: 去掉逗号后
    长度必须满足 (len - 1) % 3 == 0 且首位分组不为空。`1,23` / `12,3,456` 这类
    不合法, 说明 OCR 丢了或多了字符。

    注意这条拦不住 `643`(无逗号, 天然自洽), 所以它只是辅助防线; 末位丢失主要靠
    `_read_estimate_value` 的「数字右端贴裁框边界」告警来发现。
    """
    normalized = raw_text.translate(FULLWIDTH_NUMERIC)
    digits_and_commas = re.sub(r"[^\d,]", "", normalized)
    if "," not in digits_and_commas:
        return False
    groups = digits_and_commas.split(",")
    if groups[0] == "" or len(groups[0]) > 3:
        return False  # 首位分组缺失或超长, 由 is_partial_number_text 或调用方处理
    return any(len(group) != 3 for group in groups[1:])


def price_key_sequence(price_str: str) -> list[str]:
    """把价格字符串切分为按键序列, 可一次输入的 0000 / 00 优先整体输入。

    按**最长优先**做前缀匹配, 而不是只在「剩余整串恰好等于快捷键」时才用:
    后者会把 `1000000` 切成 `1 0 0 0000`(4 键), 前缀匹配切成 `1 0000 00`(3 键),
    而每次点击都带 after_sleep —— 少按一键就少一次 0.2 秒的等待。
    """
    shortcuts = sorted(PAD_SHORTCUTS, key=len, reverse=True)
    keys: list[str] = []
    index = 0
    while index < len(price_str):
        for shortcut in shortcuts:
            if price_str.startswith(shortcut, index):
                keys.append(shortcut)
                index += len(shortcut)
                break
        else:
            keys.append(price_str[index])
            index += 1
    return keys


def resolve_bid_prices(raw_prices: Sequence[int]) -> list[int]:
    """把 6 个每轮指定价格的原始配置值解析成可按出价序号直接取用的列表。

    每个价格对应一次出价: 第 1 次用「第1次出价价格」, 第 2 次用「第2次出价价格」, 依此类推。
    未设置(0)的回合沿用上一次已设置的价格, 因此只填前几次也能正常工作。
    第 1 次出价必须有价格, 否则返回空列表, 由调用方按配置错误处理。
    """
    resolved: list[int] = []
    current = 0
    for raw in raw_prices:
        if raw > 0:
            current = raw
        resolved.append(current)

    if not resolved or resolved[0] <= 0:
        return []
    return resolved


def validate_bid_prices(raw_prices: Sequence[int]) -> list[int]:
    """解析每轮指定价格并校验末次出价必须更高, 非法时抛 ValueError。

    游戏中第 6 次出价必须高于第 5 次, 否则这一次出价会被系统拒绝;
    留 0 沿用上一次价格会导致两者相等, 所以第 6 次必须显式填写。
    """
    prices = resolve_bid_prices(raw_prices)
    if not prices:
        raise ValueError(f"每轮指定价格未配置: {CONF_BID_PRICES[0]} 必须大于 0")
    if len(prices) < 2:
        return prices

    last_key = CONF_BID_PRICES[len(prices) - 1]
    previous_key = CONF_BID_PRICES[len(prices) - 2]
    last, previous = prices[-1], prices[-2]
    if last > previous:
        return prices

    # 区分「没填」和「填小了」, 两种情况用户要做的修改不一样。
    if raw_prices[len(prices) - 1] <= 0:
        raise ValueError(
            f"{last_key} 未设置: 沿用上一次的价格 {previous} 不会高于 "
            f"{previous_key}, 请显式填写一个更大的值"
        )
    raise ValueError(f"{last_key} ({last}) 必须大于 {previous_key} ({previous})")


def raise_offset(bid_count: int, raise_round: int) -> int | None:
    """第 bid_count 次出价的加价偏移次数, 从 1 开始; 未到加价回合时返回 None。

    raise_round=0 表示第 1 次出价就按加价算; N 表示第 N 次起才开始加价,
    之前的出价直接使用基础价 (调用方据此返回, 不告警)。
    """
    if raise_round > 0 and bid_count < raise_round:
        return None
    return bid_count if raise_round == 0 else bid_count - raise_round + 1


def raise_price(
    base_price: int,
    offset: int,
    *,
    mode: str,
    raise_value: Decimal,
) -> int | None:
    """按加价方式计算基础价经过 offset 次加价后的价格。

    全程用 Decimal 计算: 倍率模式是 `base * value ** offset`, 用原始 float 时
    「加价数值」填得稍大就会在 `value ** offset` 上抛 OverflowError(实测
    `100000 * 10.0 ** 400`)。异常会被出价循环吞成「出价异常」重试,
    价格永远算不出来, 却看不到真正的原因。

    Returns:
        计算出的整数价格; 结果超出可表示范围(溢出/非有限值)时返回 None,
        由调用方告警并回退基础价。返回值仍可能 <= 0, 有效性由调用方校验。
    """
    base = Decimal(str(base_price))
    try:
        if mode == RAISE_MODE_MULTIPLE:
            # 指数增长: 基础价 * (倍率 ^ offset).
            result = base * (raise_value**offset)
        elif mode == RAISE_MODE_PERCENT:
            # 线性增长: 基础价 * (1 + 百分比 / 100 * offset).
            result = base * (Decimal(1) + raise_value / 100 * offset)
        else:  # 自定义
            # 线性增长: 基础价 + 自定义值 * offset.
            result = base + raise_value * offset
        # 量化必须留在 try 内: Decimal 的指数范围极大, `10 ** 400` 仍是有限值,
        # is_finite() 拦不住; 但它有 405 位有效数字, 超过默认上下文精度 28,
        # 到这一步 quantize 才抛 InvalidOperation。放在 try 外等于把
        # OverflowError 换成同样会漏出的 InvalidOperation。
        # 位数粗筛(整数部分 30 位以上)提前挡掉, 避免真的把大数交给 quantize。
        rounded = (
            result.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if result.adjusted() < 30
            else None
        )
        if rounded is None:
            result = None
        else:
            result = rounded
    except (ArithmeticError, InvalidOperation, ValueError):
        result = None

    # 溢出/精度异常时 result 为 None, 或退化成非有限值(NaN / Infinity).
    if result is None or not result.is_finite():
        return None
    # 已在 try 内完成量化, 这里直接取整.
    return int(result)


def estimate_price(estimate: int, ratio: float) -> int:
    """估价 x 倍率, 与加价计算同口径: Decimal 乘法后按 ROUND_HALF_UP 取整。"""
    result = Decimal(str(estimate)) * Decimal(str(ratio))
    return int(result.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def special_round_price(
    bid_count: int, special_rounds: Iterable[int], special_price: int
) -> int | None:
    """当前出价序号命中「指定回合」且单独价格有效(> 0)时返回该价格, 否则 None。"""
    return special_price if special_price > 0 and bid_count in special_rounds else None
