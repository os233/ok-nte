"""拍卖任务配置声明: 配置键, 取值, 默认配置, 控件类型与说明文本。

本模块只承载纯声明, 不依赖任务类。`AutoBidAuctionTask` 通过类级别名暴露这些
常量, 配置面板在任务 `__init__` 里由 default_config / config_type /
config_description 三个构建器装配, 键与值必须与迁移前逐一致。
"""

# --- 拍卖配置 ---
CONF_FIXED_PRICE = "基础价"

# 藏品出售: 用一个模式下拉框统一控制, 相关子配置集中显示在它下面。
# 旧版的「启用自动清理藏品」开关与「出售藏品间隔次数」是互斥的两档, 现在合并进模式:
#   不出售           -> 完全不碰仓库
#   满仓时清理       -> 只在主界面出现「库存不足」提示时出售
#   按间隔出售       -> 每 N 轮出售一次, 满仓时提前触发
#   拍卖成功一键出售 -> 用游戏自带的一键出售, 在结算界面直接卖掉本局藏品, 不碰仓库
# 前三种走同一套「仓库流程」(满仓检测 + 品质勾选 + 确认出售), 第四种是另一条独立路径,
# 见 _uses_collection_sell / _sell_on_settlement_screen。
CONF_SELL_MODE = "出售藏品模式"
SELL_MODE_OFF = "不出售"
SELL_MODE_FULL = "满仓时清理"
SELL_MODE_ONE_CLICK = "拍卖成功一键出售"
SELL_MODE_INTERVAL = "按间隔出售"
SELL_MODES = (
    SELL_MODE_OFF,
    SELL_MODE_FULL,
    SELL_MODE_INTERVAL,
    SELL_MODE_ONE_CLICK,
)

CONF_SELL_INTERVAL = "出售藏品间隔次数"
# 两个出售品质清单都是「勾选即出售」: 勾了才卖, 没勾的一律保留。
# 用哪个清单由当日低保阶段自动决定, 见 _sell_qualities。
CONF_SELL_BEFORE_WELFARE = "未领完低保时出售品质"
CONF_SELL_AFTER_WELFARE = "已领完低保时出售品质"
SELL_QUALITY_KEYS = (CONF_SELL_BEFORE_WELFARE, CONF_SELL_AFTER_WELFARE)

# 自动加价配置.
CONF_AUTO_RAISE = "启用自动加价"
CONF_RAISE_MODE = "加价方式"
# 加价方式的取值既是下拉框标签, 又是持久化的配置值, 还被 _raise_price 当判定串用。
# 改这几个取值会让老用户的下拉框显示空白, 而且判定串失配后会静默落到「自定义」分支,
# 算出完全不同的价格 —— 所以除了改名, 还要在 _raise_mode 里加读取兜底。
RAISE_MODE_MULTIPLE = "倍率"
RAISE_MODE_CUSTOM = "自定义"
RAISE_MODE_PERCENT = "百分比"
RAISE_MODES = (RAISE_MODE_MULTIPLE, RAISE_MODE_CUSTOM, RAISE_MODE_PERCENT)
CONF_RAISE_VALUE = "加价数值"
CONF_RAISE_ROUND = "加价回合数"

# 指定回合出价配置.
CONF_SPECIAL_ROUND = "启用指定回合单独出价"
CONF_SPECIAL_ROUNDS = "指定回合(可多选)"
CONF_SPECIAL_ROUND_PRICE = "指定回合价格"

# 出价模式.
CONF_BID_MODE = "出价模式"
BID_MODE_CUSTOM = "自定义价格"
BID_MODE_LIST = "每轮指定价格"
BID_MODE_ESTIMATE = "按系统估价"
CONF_ESTIMATE_RATIO = "估价倍率"

# 每轮指定价格: 一轮拍卖最多 6 回合出价, 每次出价各自一个价格.
MAX_BID_ROUNDS = 6
CONF_BID_PRICES = tuple(f"第{index}次出价价格" for index in range(1, MAX_BID_ROUNDS + 1))

# 拍卖辅助功能: 多选框, 勾选即启用.
CONF_ASSIST_FEATURES = "启用辅助功能"
ASSIST_EMOTE = "表情包"
ASSIST_WELFARE = "低保金"
ASSIST_FEATURES = (ASSIST_EMOTE, ASSIST_WELFARE)

# 品质按钮 (白, 绿, 蓝, 紫, 橙, 红), 与 layout.QUALITY_BOXES 一一对应.
QUALITY_KEYS = ["品质白", "品质绿", "品质蓝", "品质紫", "品质橙", "品质红"]

# 指定回合下拉框候选项.
SPECIAL_ROUND_OPTIONS = [str(index) for index in range(1, 7)]


def default_config() -> dict:
    """任务默认配置, 键为上面的配置键常量, 顺序与面板控件顺序一致。"""
    return {
        # 出售藏品相关配置集中放在最前面, 由模式下拉框统一控制可见性,
        # 避免「出售间隔 / 出售品质 / 自动清理」散落在面板各处.
        CONF_SELL_MODE: SELL_MODE_ONE_CLICK,
        CONF_SELL_INTERVAL: 0,
        # 还没领完低保时只卖低价值品质: 低保金的领取前提是资产低于 10 万,
        # 而卖藏品会抬高资产, 卖多了当天剩下的低保就领不到了.
        CONF_SELL_BEFORE_WELFARE: ["品质白", "品质绿", "品质蓝"],
        # 领完当日次数后低保已无望, 这时可以放开更高价值的品质.
        CONF_SELL_AFTER_WELFARE: ["品质白", "品质绿", "品质蓝", "品质紫"],
        CONF_AUTO_RAISE: False,
        CONF_FIXED_PRICE: 1,
        CONF_BID_MODE: BID_MODE_ESTIMATE,
        CONF_ESTIMATE_RATIO: "1",
        # 每轮指定价格: 6 次出价各自一个价格, 0 表示沿用上一次的价格.
        **dict.fromkeys(CONF_BID_PRICES, 0),
        CONF_RAISE_MODE: RAISE_MODE_MULTIPLE,
        CONF_RAISE_VALUE: "1.6",
        CONF_RAISE_ROUND: 2,
        CONF_SPECIAL_ROUND: False,
        CONF_SPECIAL_ROUNDS: ["5"],
        CONF_SPECIAL_ROUND_PRICE: "66666",
        CONF_ASSIST_FEATURES: [ASSIST_WELFARE],
    }


def config_type() -> dict:
    """下拉框选项与条件子配置(控件类型)定义。"""
    return {
        # 按出价模式只展示该模式真正会用到的价格配置.
        CONF_BID_MODE: {
            "options": [BID_MODE_CUSTOM, BID_MODE_LIST, BID_MODE_ESTIMATE],
            "sub_configs": {
                BID_MODE_CUSTOM: [
                    CONF_FIXED_PRICE,
                    CONF_AUTO_RAISE,
                    CONF_RAISE_MODE,
                    CONF_RAISE_ROUND,
                    CONF_SPECIAL_ROUND,
                ],
                BID_MODE_LIST: list(CONF_BID_PRICES),
                BID_MODE_ESTIMATE: [CONF_ESTIMATE_RATIO],
            },
        },
        CONF_RAISE_MODE: {
            "options": list(RAISE_MODES),
            "sub_configs": {
                RAISE_MODE_MULTIPLE: [CONF_RAISE_VALUE],
                RAISE_MODE_CUSTOM: [CONF_RAISE_VALUE],
                RAISE_MODE_PERCENT: [CONF_RAISE_VALUE],
            },
        },
        CONF_SELL_BEFORE_WELFARE: {
            "type": "multi_selection",
            "options": list(QUALITY_KEYS),
        },
        CONF_SELL_AFTER_WELFARE: {
            "type": "multi_selection",
            "options": list(QUALITY_KEYS),
        },
        CONF_ASSIST_FEATURES: {
            "type": "multi_selection",
            "options": list(ASSIST_FEATURES),
        },
        # 出售模式把「出售间隔 / 两个出售品质清单」收在同一处:
        # 选「不出售」时这些子项全部隐藏, 面板只剩一个下拉框.
        CONF_SELL_MODE: {
            "options": list(SELL_MODES),
            "sub_configs": {
                SELL_MODE_OFF: [],
                SELL_MODE_FULL: list(SELL_QUALITY_KEYS),
                SELL_MODE_INTERVAL: [
                    CONF_SELL_INTERVAL,
                    *SELL_QUALITY_KEYS,
                ],
                # 一键出售用游戏自带的整包出售, 没有品质勾选也没有间隔, 因此没有子项.
                SELL_MODE_ONE_CLICK: [],
            },
        },
        # 仅在开关启用时显示指定回合配置.
        CONF_SPECIAL_ROUND: {
            "sub_configs": {
                True: [CONF_SPECIAL_ROUNDS, CONF_SPECIAL_ROUND_PRICE],
            }
        },
        CONF_SPECIAL_ROUNDS: {
            "type": "multi_selection",
            "options": list(SPECIAL_ROUND_OPTIONS),
        },
    }


def config_description() -> dict:
    """配置说明, 按 default_config 的顺序排列, 与面板上的控件顺序一致。

    每条只写「标签本身看不出来的信息」: 做什么, 硬约束, 以及读不到时的回退行为。
    """
    descriptions = {
        # --- 藏品出售 ---
        CONF_SELL_MODE: "满仓后无法继续出价, 建议至少选「满仓时清理」; 选「不出售」"
        "则完全不碰仓库; 选「拍卖成功一键出售」用游戏自带的一键出售在结算界面直接"
        "卖掉本局藏品, 不做满仓检测也不筛选品质",
        CONF_SELL_INTERVAL: "每 N 轮出售一次, 满仓时提前触发; 填 0 会退化成只按"
        "满仓清理",
        CONF_SELL_BEFORE_WELFARE: "勾选即出售: 今日低保次数还没领满时卖这些"
        "品质, 没勾的一律保留; 低保金的领取前提是资产低于 10 万, 而卖藏品会抬高"
        "资产, 所以这一档只勾低价值品质",
        CONF_SELL_AFTER_WELFARE: "勾选即出售: 今日低保次数领满后卖这些品质;"
        "领满之后当天再也领不到低保, 可以放开勾选更高价值的品质; 一般应包含"
        "「未领完低保时出售品质」勾选的全部品质, 否则领满后反而卖得更少;"
        "切换依据是低保金弹窗上的次数读数, 需勾选「低保金」辅助功能且资产低于"
        "10 万弹窗才会被打开, 读不到读数时一律按未领完处理(保守, 少卖不亏)",
        # --- 出价 ---
        CONF_AUTO_RAISE: "在基础价之上按「加价方式」逐次提高出价",
        CONF_FIXED_PRICE: "自定义价格的基准价; 必须为正整数, 否则任务不会启动",
        CONF_BID_MODE: "自定义价格按基础价与加价方式定价, 每轮指定价格按出价序号"
        "逐个取值, 按系统估价读界面右上角估价并乘倍率",
        CONF_ESTIMATE_RATIO: "出价 = 当前估价 x 倍率, 需为正数; 估价读不出时回退到"
        "「基础价」",
        CONF_RAISE_MODE: "倍率: 基础价x倍率^次数; 百分比: 基础价x(1+百分比/100x次数); "
        "自定义: 基础价+数值x次数",
        CONF_RAISE_VALUE: "三种加价方式共用, 含义随方式变化(倍率 / 百分比 / 每次"
        "增加额), 支持小数",
        CONF_RAISE_ROUND: "第几次出价开始加价; 0 表示第 1 次就按加价算, N 表示第 N 次"
        "起才开始加价",
        CONF_SPECIAL_ROUND: "勾选的回合直接使用「指定回合价格」, 跳过基础价与加价计算",
        CONF_SPECIAL_ROUNDS: "这些序号的出价使用单独价格, 序号从 1 开始",
        CONF_SPECIAL_ROUND_PRICE: "指定回合使用的价格, 需为正整数; 留空或填 0 时该"
        "功能不生效",
        # --- 辅助功能 ---
        CONF_ASSIST_FEATURES: "勾选即启用; 表情包: 每次出价成功后发送表情菜单里的"
        "第一个表情; 低保金: 主界面资产低于 10 万时自动领取",
    }

    # 每轮指定价格: 6 个价格依次对应第 1~6 次出价.
    for index, key in enumerate(CONF_BID_PRICES, start=1):
        if index == 1:
            description = "第 1 次出价的价格, 必须大于 0, 否则任务不会启动"
        elif index == MAX_BID_ROUNDS:
            description = (
                f"第 {index} 次出价的价格, 必须显式填写且高于第 {index - 1} 次,"
                " 否则会被系统拒绝"
            )
        else:
            description = f"第 {index} 次出价的价格; 留 0 则沿用上一次的价格"
        descriptions[key] = description
    return descriptions


def _inst_line(text: str, color: str = "", *, bold: bool = False, indent: int = 0):
    content = f"{'&nbsp;' * (indent * 4)}{text}"
    if bold:
        content = f"<strong>{content}</strong>"
    return f'<span style="color:{color};">{content}</span>'


# 任务卡上的「说明」按钮内容: ok-script 在 task.instructions 非空时显示该按钮,
# 点击后用富文本弹窗渲染, 支持 <strong> / <span style> / <a href>, 换行由框架转 <br>。
# 本任务只支持 zh_CN(supported_languages), 因此不准备英文版本。
# 正文里带「」的串与面板上的配置标签同名, 便于用户按标签在面板上对号入座。
#
# ⚠️ 行数是硬约束, 加内容前先看这里: qfluentwidgets 的 Dialog 用
# QVBoxLayout.SetMinimumSize, 既没有滚动区也没有高度钳制, 而框架的
# show_instructions 会 hide() 掉「取消」, 只留内容下方的「确定」按钮。
# 上限由 MaskDialogBase.setGeometry(0, 0, parent.width(), parent.height()) 决定 ——
# 遮罩跟着**父窗口**(src/config.py 的 window_size = 1200x800)而不是屏幕走, 弹窗高于
# 父窗口时居中溢出, 上下两端一起被裁, 底部的按钮行随之消失, 用户直接关不掉弹窗。
# 实测: 每行约 16px + 185px 固定开销(标题/边距/按钮行), 旧版 50 行 = 985px,
# 在 800px 父窗口下溢出 185px; 硬上限约 38 行。因此这里也不插空行(_inst_gap)——
# 空行同样吃高度, 分段靠标题本身。上限由测试的长度用例守住。
# ruff: disable[E501]
INST = "<br>".join(
    [
        _inst_line("📍 使用前提", "#FF5555", bold=True),
        _inst_line("在大世界或拍卖主界面启动均可, 会自动进入; 「循环次数」填 0 = 一直运行", indent=1),
        _inst_line("掉线被踢回大世界时会自动回场(F5 都市大亨 → 都市闲趣 → 即刻落槌)", indent=1),
        _inst_line("💰 「出价模式」三选一", "#FF5555", bold=True),
        _inst_line(
            "按系统估价: 估价 x「估价倍率」, 读不出时回退「基础价」", "#FE821D", bold=True, indent=1
        ),
        _inst_line(
            "自定义价格: 以「基础价」为基准, 开「启用自动加价」后按「加价方式 / 加价数值 /",
            "#FE821D",
            bold=True,
            indent=1,
        ),
        _inst_line(
            "加价回合数」逐次提价, 也可用「启用指定回合单独出价」走「指定回合价格」", indent=2
        ),
        _inst_line(
            "每轮指定价格: 「第1次出价价格」~「第6次出价价格」, 填 0 沿用上次",
            "#FE821D",
            bold=True,
            indent=1,
        ),
        _inst_line("第 6 次必须高于第 5 次, 否则会被系统拒绝", "#FF5555", bold=True, indent=2),
        _inst_line("📦 「出售藏品模式」四选一", "#FF5555", bold=True),
        _inst_line(
            "不出售 / 满仓时清理 / 按间隔出售 / 拍卖成功一键出售", "#FE821D", bold=True, indent=1
        ),
        _inst_line(
            "满仓后无法继续出价, 建议至少选「满仓时清理」; 「出售藏品间隔次数」填 0 = 只按满仓清理",
            indent=2,
        ),
        _inst_line(
            "「未领完低保时出售品质 / 已领完低保时出售品质」勾选即出售, 没勾的一律保留,"
            " 两个都不勾 = 不卖任何藏品",
            indent=2,
        ),
        _inst_line(
            "清单按低保「今日次数领满」自动切换: 没领满时别勾高价值品质, 卖藏品会抬高资产,"
            " 资产过 10 万就领不到低保",
            indent=2,
        ),
        _inst_line(
            "✨ 「启用辅助功能」: 表情包 = 出价后发表情; 低保金 = 资产低于 10 万时领取",
            "#FF5555",
            bold=True,
        ),
        _inst_line("🔄 升级后必看", "#FF5555", bold=True),
        _inst_line(
            "旧版「启用自动清理藏品 / 启用表情包 / 启用低保金 / 保留藏品品质 /",
            "#FF5555",
            bold=True,
            indent=1,
        ),
        _inst_line(
            "满仓或领低保后追加出售品质」已废弃, 不再自动换算, 首次启动一律按上面的默认值重建",
            "#FF5555",
            bold=True,
            indent=2,
        ),
        _inst_line(
            "旧配置里的无效「加价方式」不再迁移, 会按默认「倍率」处理; 下拉框若显示空白请重选", indent=2
        ),
        _inst_line("「按系统估价」读估价会多等约 1 秒(防误读成 1/10 价格), 不是卡住", indent=2),
        _inst_line(
            "想让本机配置回到这套默认: 面板点「重置配置」, 它会清掉你填过的值(含价格),",
            "#FF5555",
            bold=True,
            indent=1,
        ),
        _inst_line(
            "「循环次数」回到 0(一直运行), 「出售藏品模式」回到「不出售」",
            "#FF5555",
            bold=True,
            indent=2,
        ),
        _inst_line(
            "⚠️ 出价失败会自动重试, 单轮失败不影响后续轮次; 仓库卖不掉时会放宽出售清单", indent=1
        ),
    ]
)
# ruff: enable[E501]
