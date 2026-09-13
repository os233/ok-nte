"""AutoBidAuctionTask 的离线帧验证。

分两部分:

1. `TestAuctionRegionGeometry` —— **不需要截图**, 总是运行。
   校验 25 个 `BOX_*` 相对比例常量良构(坐标单调、落在屏幕内),
   并确认捕获桩 + OCR 调用链能跑通。

2. `TestAutoBidAuctionFrames` —— 在真实拍卖截图上验证 OCR 正则与区域比例。
   截图放在 `tests/images/auction/<stem>.png`, 缺图的用例自动跳过。
   本类在**不开游戏**的前提下验证"区域是否框对 + 正则是否匹配得上"。

帧目录刻意**不放在 `screenshots/`**: ok-script 初始化时会清空配置里的
`screenshots_folder`, 放在那里会被每次测试运行删掉。

当前已覆盖的画面(11 张, 均 1920x1080):

| 文件              | 画面                                                       |
| ----------------- | ---------------------------------------------------------- |
| `main.png`        | 拍卖主界面: 开始匹配 / 我的资产 / 低保金 / 藏品仓库         |
| `insufficient.png`| 主界面 + 库存不足横幅(剩余空间少于200格)                    |
| `confirm.png`     | 入场费确认弹窗(提示: 扣除入场费0, 是否继续)                 |
| `exception.png`   | 异常确认弹窗(提示: 出价1,000,000疑似异常过高, 是否确认)     |
| `bid.png`         | 出价界面: 出价 / 放弃 / 竞拍第1回合                         |
| `bid_panel.png`   | 数字面板(未输入): 确认出价 / 清空 / 上轮出价 / 可输入范围   |
| `price_result.png`| 数字面板(已输入 1,000,000): 价格结果区 / 确认出价            |
| `result.png`      | 竞拍结束: 跳过动画 / 退出(87s) / 资产                       |
| `result_skip.png` | 竞拍结束(仅跳过动画, 尚未出现退出按钮)                       |
| `welfare.png`     | 低保金领取弹窗: 取消 / 领取                                 |
| `warehouse.png`   | 藏品仓库: 标题 / 出售                                       |

截图里的资产值各不相同(main 16155238 / insufficient 15487814 / result_skip 16061906),
正好顺带验证解析没有写死常量。

运行:

    .venv/Scripts/python.exe -m unittest tests.TestAutoBidAuctionFrames -v
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from ok.test.TaskTestCase import TaskTestCase
from PIL import Image

from src.config import config
from src.tasks import AutoBidAuctionTask as auction_module
from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask

config["debug"] = True

FRAME_DIR = Path("tests") / "images" / "auction"
REFERENCE_SIZE = (1920, 1080)

# 尚未提供的画面。缺图时对应用例自动跳过, 补齐截图后自动启用。
# 目前已全部补齐; 保留这个机制, 以后新增画面先登记再补图。
PENDING_FRAMES: dict[str, str] = {}

# (截图 stem, 说明, Box 常量名, 正则常量名) —— 该区域应能匹配到该正则
REGION_HITS = [
    # main.png —— 拍卖主界面
    ("main", "开始匹配按钮", "BOX_MATCH", "RE_MATCH"),
    ("main", "主界面资产标题", "BOX_MAIN_ASSET_TITLE", "RE_MAIN_ASSET_TITLE"),
    ("main", "低保金入口", "BOX_WELFARE_BTN", "RE_WELFARE"),
    ("main", "藏品仓库入口", "BOX_WAREHOUSE_BTN", "RE_WAREHOUSE"),
    # insufficient.png —— 主界面 + 库存不足横幅
    ("insufficient", "库存不足提示", "BOX_INSUFFICIENT", "RE_COLLECTION_INSUFFICIENT"),
    ("insufficient", "主界面资产标题", "BOX_MAIN_ASSET_TITLE", "RE_MAIN_ASSET_TITLE"),
    ("insufficient", "开始匹配按钮", "BOX_MATCH", "RE_MATCH"),
    ("insufficient", "藏品仓库入口", "BOX_WAREHOUSE_BTN", "RE_WAREHOUSE"),
    ("insufficient", "低保金入口", "BOX_WELFARE_BTN", "RE_WELFARE"),
    # confirm.png —— 入场费确认弹窗
    ("confirm", "确认按钮", "BOX_CONFIRM", "RE_CONFIRM"),
    ("confirm", "取消按钮", "BOX_CANCEL", "RE_CANCEL"),
    ("confirm", "异常确认框区域", "BOX_EXCEPTION_AREA", "RE_CONFIRM_ANY"),
    # exception.png —— 异常确认弹窗(出价异常过高)
    ("exception", "异常弹窗确认按钮", "BOX_CONFIRM", "RE_CONFIRM"),
    ("exception", "异常弹窗取消按钮", "BOX_CANCEL", "RE_CANCEL"),
    ("exception", "异常确认框区域", "BOX_EXCEPTION_AREA", "RE_CONFIRM_ANY"),
    # bid.png —— 出价界面
    ("bid", "出价按钮", "BOX_BID", "RE_BID"),
    # bid_panel.png —— 数字面板(未输入)
    ("bid_panel", "确认出价按钮", "BOX_BID_CONFIRM", "RE_BID_CONFIRM"),
    ("bid_panel", "上轮出价按钮", "BOX_LAST_BID", "RE_BID"),
    # price_result.png —— 数字面板(已输入价格)
    ("price_result", "确认出价按钮", "BOX_BID_CONFIRM", "RE_BID_CONFIRM"),
    ("price_result", "上轮出价按钮", "BOX_LAST_BID", "RE_BID"),
    ("price_result", "出价按钮", "BOX_BID", "RE_BID"),
    # result.png / result_skip.png —— 竞拍结束
    ("result", "跳过动画按钮", "BOX_SKIP_AREA", "RE_SKIP"),
    ("result", "退出拍卖按钮", "BOX_EXIT", "RE_EXIT"),
    ("result_skip", "跳过动画按钮", "BOX_SKIP_AREA", "RE_SKIP"),
    # welfare.png —— 低保金领取弹窗
    ("welfare", "领取按钮", "BOX_CLAIM", "RE_CLAIM"),
    ("welfare", "取消按钮", "BOX_CANCEL", "RE_CANCEL"),
    # warehouse.png —— 藏品仓库
    ("warehouse", "藏品仓库标题", "BOX_WAREHOUSE_TITLE", "RE_WAREHOUSE"),
]

# (截图 stem, 说明, Box 常量名, 期望解析出的整数)
REGION_NUMBERS = [
    ("main", "主界面资产值", "BOX_MAIN_ASSET", 16155238),
    ("insufficient", "主界面资产值(库存不足时)", "BOX_MAIN_ASSET", 15487814),
    ("bid", "出价界面资产值", "BOX_ASSET_VALUE", 16155238),
    ("bid_panel", "数字面板资产值", "BOX_ASSET_VALUE", 16155238),
    ("bid_panel", "价格结果区(未输入, 显示可输入范围提示)", "BOX_PRICE_RESULT", 16155238),
    ("price_result", "价格结果区(已输入价格)", "BOX_PRICE_RESULT", 1000000),
    ("price_result", "数字面板资产值", "BOX_ASSET_VALUE", 16155238),
    ("result", "结算界面资产值", "BOX_ASSET_VALUE", 16155238),
    ("result_skip", "结算界面资产值", "BOX_ASSET_VALUE", 16061906),
]

_REFERENCED_FRAMES = sorted(
    {stem for stem, *_ in REGION_HITS} | {stem for stem, *_ in REGION_NUMBERS}
)

# 四个界面状态判定, 与 AutoBidAuctionTask._is_*_screen 一一对应
STATE_CHECKERS = {
    "match": "_is_match_screen",
    "confirm": "_is_confirm_screen",
    "bid": "_is_bid_screen",
    "skip": "_is_skip_screen",
}

# (截图 stem, 期望命中的状态名; None 表示四个状态都不应命中)
# 实测每个画面都只命中一个状态, 且非拍卖阶段的两个画面一个都不命中。
# 注意 exception.png(出价异常弹窗) 命中的是 confirm —— 弹窗按钮与入场费确认框同构,
# 任务走的也是同一条"点确认"分支。
SCREEN_STATES = [
    ("main", "match"),
    ("insufficient", "match"),
    ("confirm", "confirm"),
    ("exception", "confirm"),
    ("bid", "bid"),
    ("bid_panel", "bid"),
    ("price_result", "bid"),
    ("result", "skip"),
    ("result_skip", "skip"),
    ("warehouse", None),
    ("welfare", None),
]

# (截图 stem, 说明, Box 常量名, 正则常量名) —— 该区域**不应**命中该正则
REGION_MISSES = [
    # 任务用 BOX_MAIN_ASSET_TITLE 判断"已回到主界面"; 仓库界面必须判否,
    # 否则 _claim_welfare_if_needed 会拿着仓库界面的数字当资产值。
    ("warehouse", "仓库界面不应被误判为主界面", "BOX_MAIN_ASSET_TITLE", "RE_MAIN_ASSET_TITLE"),
]


def _box_ratios():
    """返回所有 BOX_* 相对比例常量, 按名称排序。"""
    return sorted(
        (name, value)
        for name, value in vars(AutoBidAuctionTask).items()
        if name.startswith("BOX_") and isinstance(value, tuple) and len(value) == 4
    )


def _has_any_frame():
    return FRAME_DIR.is_dir() and any(FRAME_DIR.glob("*.png"))


# --------------------------------------------------------------------------
# 1. 无需截图: 区域几何 + 管道自检 + 帧清单自检
# --------------------------------------------------------------------------
class TestAuctionRegionGeometry(TaskTestCase):
    """不依赖真实截图, 校验坐标常量与 OCR 调用链。"""

    task_class = AutoBidAuctionTask
    config = config

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._frame = Path(cls._tmp.name) / "blank_1920x1080.png"
        Image.new("RGB", REFERENCE_SIZE, "white").save(cls._frame)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def test_ratio_constants_are_well_formed(self):
        """每个 BOX_* 必须是递增且在屏幕范围内的比例矩形。"""
        for name, (x, y, to_x, to_y) in _box_ratios():
            with self.subTest(box=name):
                self.assertLess(x, to_x, f"{name}: to_x 必须大于 x")
                self.assertLess(y, to_y, f"{name}: to_y 必须大于 y")
                self.assertGreaterEqual(x, 0.0, f"{name}: x 越界")
                self.assertGreaterEqual(y, 0.0, f"{name}: y 越界")
                self.assertLessEqual(to_x, 1.0, f"{name}: to_x 越界")
                self.assertLessEqual(to_y, 1.0, f"{name}: to_y 越界")

    def test_regions_fall_inside_screen(self):
        """按 1920x1080 换算后, 每个区域都必须有正的宽高且不超出画面。"""
        self.set_image(str(self._frame))
        screen_width, screen_height = REFERENCE_SIZE

        for name, ratios in _box_ratios():
            with self.subTest(box=name):
                box = self.task.box_of_screen(*ratios)
                self.assertGreater(box.width, 0, f"{name}: 宽度为 0")
                self.assertGreater(box.height, 0, f"{name}: 高度为 0")
                self.assertGreaterEqual(box.x, 0, f"{name}: x 为负")
                self.assertGreaterEqual(box.y, 0, f"{name}: y 为负")
                self.assertLessEqual(box.x + box.width, screen_width, f"{name}: 右边越界")
                self.assertLessEqual(box.y + box.height, screen_height, f"{name}: 下边越界")

    def test_ocr_runs_on_every_region(self):
        """OCR 调用链可用: 对每个区域调用一次不应抛异常。"""
        self.set_image(str(self._frame))
        for name, ratios in _box_ratios():
            with self.subTest(box=name):
                self.task.ocr(box=self.task.box_of_screen(*ratios), match=auction_module.RE_NUMBER)

    def test_every_box_constant_is_wired_into_auction_boxes(self):
        """25 个 BOX_* 常量必须与 AuctionBoxes 字段一一对应, 且换算结果一致。

        只校验常量良构是不够的: 常量存在但忘了塞进 `_build_boxes()` 时,
        任务会拿着 None 去点击/OCR, 而比例常量测试依然全绿。
        """
        self.set_image(str(self._frame))
        boxes = self.task._build_boxes()
        constants = _box_ratios()
        field_names = {field.name for field in dataclasses.fields(auction_module.AuctionBoxes)}

        self.assertEqual(
            len(field_names),
            len(constants),
            f"BOX_* 常量 {len(constants)} 个, 但 AuctionBoxes 有 {len(field_names)} 个字段: "
            f"{sorted(field_names)}",
        )

        for name, ratios in constants:
            field_name = name[len("BOX_") :].lower()
            with self.subTest(box=name):
                self.assertIn(field_name, field_names, f"{name} 没有对应的 AuctionBoxes 字段")
                expected = self.task.box_of_screen(*ratios)
                actual = getattr(boxes, field_name)
                self.assertEqual(
                    (actual.x, actual.y, actual.width, actual.height),
                    (expected.x, expected.y, expected.width, expected.height),
                    f"{name} 未按比例常量正确装配到 boxes.{field_name}",
                )

    def test_referenced_frames_exist_or_are_declared_pending(self):
        """引用的截图要么存在, 要么显式登记在 PENDING_FRAMES —— 防文件名写错后静默跳过。"""
        if not FRAME_DIR.is_dir():
            self.skipTest(f"缺少 {FRAME_DIR} 目录, 跳过帧清单自检")
        missing = [stem for stem in _REFERENCED_FRAMES if not (FRAME_DIR / f"{stem}.png").exists()]
        unexpected = [stem for stem in missing if stem not in PENDING_FRAMES]
        self.assertEqual(
            unexpected,
            [],
            f"以下截图缺失且未登记在 PENDING_FRAMES: {unexpected}。"
            f"请补齐截图, 或把它加入 PENDING_FRAMES 并说明原因。",
        )


# --------------------------------------------------------------------------
# 2. 需要截图: 真实画面上的 OCR 与区域校验
# --------------------------------------------------------------------------
def _make_region_hit_test(frame_stem, desc, box_name, pattern_name):
    def test(self):
        self._load(frame_stem)
        pattern = getattr(auction_module, pattern_name)
        box = self.task.box_of_screen(*getattr(AutoBidAuctionTask, box_name))
        hits = self.task.ocr(box=box, match=pattern)
        self.assertTrue(
            hits,
            f"{desc}: {box_name} 区域未匹配到 {pattern_name} ({pattern.pattern!r})。"
            f"可能是区域比例框错了位置, 或 OCR 阈值不合适。",
        )

    test.__doc__ = f"{frame_stem}.png -> {desc} ({box_name} / {pattern_name})"
    return test


def _make_region_number_test(frame_stem, desc, box_name, expected):
    def test(self):
        self._load(frame_stem)
        box = self.task.box_of_screen(*getattr(AutoBidAuctionTask, box_name))
        hits = self.task.ocr(box=box, match=auction_module.RE_NUMBER)
        self.assertTrue(hits, f"{desc}: {box_name} 区域未 OCR 到任何数字")

        raw_text = "".join(hit.name for hit in hits)
        value = AutoBidAuctionTask._parse_asset_value(raw_text)
        self.assertEqual(
            value,
            expected,
            f"{desc}: {box_name} 解析结果不符, OCR 原文 {raw_text!r}",
        )

    test.__doc__ = f"{frame_stem}.png -> {desc} ({box_name}) 解析为 {expected}"
    return test


def _make_region_miss_test(frame_stem, desc, box_name, pattern_name):
    def test(self):
        self._load(frame_stem)
        pattern = getattr(auction_module, pattern_name)
        box = self.task.box_of_screen(*getattr(AutoBidAuctionTask, box_name))
        hits = self.task.ocr(box=box, match=pattern)
        found = [hit.name for hit in hits]
        self.assertFalse(
            hits,
            f"{desc}: {box_name} 不应命中 {pattern_name} ({pattern.pattern!r}), 实际识别到 {found}",
        )

    test.__doc__ = f"{frame_stem}.png -> {desc} ({box_name} 不应命中 {pattern_name})"
    return test


def _make_screen_state_test(frame_stem, expected_state):
    def test(self):
        self._load(frame_stem)
        boxes = self.task._build_boxes()
        detected = {
            state
            for state, method_name in STATE_CHECKERS.items()
            if getattr(self.task, method_name)(boxes)
        }
        expected = {expected_state} if expected_state else set()
        self.assertEqual(
            detected,
            expected,
            f"{frame_stem}.png 的状态判定不符: 期望 {expected or '无'}, 实际 {detected or '无'}",
        )

    test.__doc__ = f"{frame_stem}.png -> 界面状态判定应为 {expected_state or '无'}"
    return test


@unittest.skipUnless(_has_any_frame(), f"缺少 {FRAME_DIR}/*.png 截图, 跳过离线帧验证")
class TestAutoBidAuctionFrames(TaskTestCase):
    """在真实拍卖截图上验证 OCR 正则、区域比例与界面状态判定。"""

    task_class = AutoBidAuctionTask
    config = config

    def _load(self, frame_stem):
        path = FRAME_DIR / f"{frame_stem}.png"
        if not path.exists():
            reason = PENDING_FRAMES.get(frame_stem, "截图缺失")
            self.skipTest(f"缺少截图 {path} ({reason})")
        self.set_image(str(path))

    def test_main_asset_readable_via_wait_ocr(self):
        """主界面资产值走通 `_read_asset_value` 的 wait_ocr + 解析全链路。

        两个画面的资产值不同, 顺带确认解析没有写死常量。
        """
        for frame_stem, expected in (("main", 16155238), ("insufficient", 15487814)):
            with self.subTest(frame=frame_stem):
                self._load(frame_stem)
                boxes = self.task._build_boxes()
                self.assertEqual(self.task._read_asset_value(boxes.main_asset, 5.0), expected)


_registered_cases = []


def _register(cls, name, test):
    """注册动态用例; 同名时加数字后缀, 避免静默覆盖。"""
    unique = name
    index = 2
    while hasattr(cls, unique):
        unique = f"{name}_{index}"
        index += 1
    setattr(cls, unique, test)
    _registered_cases.append(unique)


for _stem, _desc, _box, _pattern in REGION_HITS:
    _register(
        TestAutoBidAuctionFrames,
        f"test_region_{_stem}_{_box.lower()}",
        _make_region_hit_test(_stem, _desc, _box, _pattern),
    )

for _stem, _desc, _box, _expected in REGION_NUMBERS:
    _register(
        TestAutoBidAuctionFrames,
        f"test_number_{_stem}_{_box.lower()}",
        _make_region_number_test(_stem, _desc, _box, _expected),
    )

for _stem, _desc, _box, _pattern in REGION_MISSES:
    _register(
        TestAutoBidAuctionFrames,
        f"test_miss_{_stem}_{_box.lower()}",
        _make_region_miss_test(_stem, _desc, _box, _pattern),
    )

for _stem, _expected_state in SCREEN_STATES:
    _register(
        TestAutoBidAuctionFrames,
        f"test_state_{_stem}",
        _make_screen_state_test(_stem, _expected_state),
    )

# 动态注册容易因同名而静默丢用例, 这里显式兜底。
_expected_cases = len(REGION_HITS) + len(REGION_NUMBERS) + len(REGION_MISSES) + len(SCREEN_STATES)
if len(_registered_cases) != _expected_cases:
    raise RuntimeError(
        f"动态用例数不符: 期望 {_expected_cases}, 实际 {len(_registered_cases)}"
        f" (可能存在同名覆盖): {sorted(_registered_cases)}"
    )


if __name__ == "__main__":
    unittest.main()
