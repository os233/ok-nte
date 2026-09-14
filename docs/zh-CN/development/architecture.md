# 架构与模块开发指南

本文面向首次参与 ok-nte 开发的贡献者，从整体架构讲到各模块职责，再到"新增一个任务 / 角色 / 界面"的实操步骤。战斗 planner 的深入内容见 [Combat Planner 开发指南](combat-planner.md)，场景流转见 [SceneFlow 作者指南](scene_flow.md)，环境搭建与验证见 [从源码运行](running-from-source.md) 与 [贡献与验证](contributing.md)。

## 1. 项目定位与工作原理

ok-nte 是面向《异环》的 Windows 桌面自动化工具，基于 [ok-script](https://github.com/ok-oldking/ok-script) 框架开发。它只通过**用户可见的界面和系统输出信号**与游戏交互：

```
截图 ──→ 识别 ──→ 决策 ──→ 模拟键鼠输入
 ↑        (模板匹配 / OCR / 目标检测)     │
 └──────────────── 循环 ←───────────────┘
```

三条不可破坏的安全边界（详见根目录 `AGENTS.md`）：

1. **不读内存、不改游戏文件、不注入进程**。截图用 Windows 图形捕获 API，输入用 PostMessage 普通窗口消息，音频用 WASAPI 系统层回环捕获。
2. **支持后台运行**。游戏窗口在后台时仍可截图与发指令，因此所有识别与输入路径都不能假设窗口在前台。
3. **坐标使用相对比例**。用 `Box` / 屏幕宽高比例表达位置，不硬编码单一分辨率像素；支持 16:9、1920x1080 及更高分辨率。

## 2. 启动流程

`main.py` 只做三件事，不含业务逻辑：

1. 导入 `src/config.py` 中的 `config` 字典（全局注册表）。
2. 调用 `src/patches/startup_patches.py` 的 `install_startup_patches()` 安装兼容性补丁。
3. 构造 `ok.OK(config)` 并调用 `start()`，此后由 ok-script 框架接管：创建 Qt 界面、启动截图线程、按配置调度任务。

`main_debug.py` 是调试入口，输出更详细的日志。

### src/config.py：全局注册表

这是理解项目的第一个文件，框架的一切行为都由它配置：

| 键 | 作用 |
| --- | --- |
| `global_configs` | 全局设置项列表（按键映射、月卡检查、声音触发、鼠标防移动、后台音频路由） |
| `onetime_tasks` | 一次性任务列表，用户点击按钮执行一次 |
| `trigger_tasks` | 触发任务列表，勾选后常驻循环执行 |
| `custom_tabs` | 自定义 UI 标签页（日常配置、礼物管理、角色中心、弹琴） |
| `scene` | 场景状态单例（`NTEScene`） |
| `windows` | 游戏窗口捕获与交互方式（`GAME_EXE`、`UnrealWindow`、`NTEInteraction`、WGC/BitBlt 截图） |
| `ocr` | OCR 引擎（onnxocr + OpenVINO） |
| `template_matching` | 模板匹配配置，`coco_feature_json` 指向 `assets/coco_annotations.json` |
| `template_tab` | 模板标签页设置，`generate_label_enum` 会自动生成 `src/Labels.py` 的枚举 |
| `my_app` | 全局单例（`src/globals.py` 的 `Globals`），存放加载的模型等重资源，经 `og.my_app` 访问 |

新增任务、标签页或全局设置时，必须在这里注册。

## 3. 总体分层

```
┌─────────────────────────── src/ui/ ────────────────────────────┐
│  展示层：自定义标签页、配置编辑，只做轻量协调，不做重业务            │
├────────────────────────── src/tasks/ ──────────────────────────┤
│  业务层：一次性任务 (daily/ 等) 与触发任务 (trigger/)，            │
│  公共能力下沉到 mixin/，基类为 BaseNTETask                        │
├────────────────────── src/combat/ + src/char/ ─────────────────┤
│  战斗层：CombatCheck 状态判定、BaseCombatTask 循环、              │
│  planner 动作规划、BaseChar 角色实现                              │
├────────────────────── src/scene/ ──────────────────────────────┤
│  场景层：NTEScene 高层状态缓存、ScreenPosition 位置抽象            │
├────────────────────── 基础能力层 ───────────────────────────────┤
│  interaction/ 键鼠   vision/ 检测   sound_trigger/ 音频          │
│  utils/ 图像工具     midi_player/ 弹琴   gifts/ coffee/ heist_path/ │
└────────────────────────────────────────────────────────────────┘
```

依赖方向自上而下：任务层调用战斗层与基础能力层，不允许反向依赖；mixin 之间不互相依赖私有实现。

## 4. 任务体系

### 4.1 两种任务

| 类型 | 基类 | 生命周期 |
| --- | --- | --- |
| 一次性任务 | `NTEOneTimeTask` + `BaseNTETask`，注册在 `onetime_tasks` | 用户点击后执行一次 `run()` |
| 触发任务 | `TriggerTask`（常与 `BaseCombatTask` 组合），注册在 `trigger_tasks` | 勾选后按 `trigger_interval` 秒间隔反复执行 `run()` |

`NTEOneTimeTask.run()` 是一次性任务的统一前置检查：确认截图就绪（`scene.game_capture_ready()`）、执行器已连接（`executor.connected()`）、激活交互方式、检查月卡弹窗。任何前置失败都会抛出 `TaskDisabledException` 中止任务。

触发任务必须轻量、可中断：`trigger_interval` 不要过低（自动战斗为 0.1 秒属于特例），循环体内要有退出条件，避免阻塞 executor。

### 4.2 BaseNTETask 与 mixin

`src/tasks/BaseNTETask.py` 组装了全部公共能力，继承顺序：

```python
class BaseNTETask(
    SceneFlowMixin,   # wait_until 等待与场景流转
    CharUIMixin,      # 队伍角色识别、当前角色索引、元素识别
    MovementMixin,    # walk_to_box 行走寻路
    VisionMixin,      # find_sift_feature、find_rotated_template 等视觉查找
    RoundMixin,       # 多轮次任务的状态与统计
    OgMixin,          # 前后台切换、配置 UI 同步
    LogGateMixin,     # 日志限流
    BaseTask,         # ok-script 框架基类（截图、点击、OCR、sleep 等）
):
```

任务代码只面向 `BaseNTETask` 提供的合并接口编程，不感知 mixin 细节。往 mixin 添加功能前先确认它确实是**跨任务通用**的，任务私有逻辑留在任务文件内。

### 4.3 任务配置项

任务通过 `default_config` 声明配置默认值，`config_description` 提供界面上的说明文字：

```python
self.default_config.update({self.CONF_CLAIM_MAIL: True})
self.config_description = {
    self.CONF_CLAIM_MAIL: "领取游戏内邮件附件",
}
```

用户可见字符串需要同步 i18n 翻译（`i18n/` 目录，参考 `translate-task-i18n` 技能流程）。

### 4.4 多轮次任务

可重复执行的动作使用 `RoundMixin`：`add_rounds_config()` 生成"执行次数"配置，`start_rounds()` / `begin_round()` 控制循环，`add_success()` / `add_failed()` 记录结果，`finish_rounds()` 收尾并汇报。

## 5. 场景层

- `src/scene/NTEScene.py`：高层状态缓存，记录"是否在队伍界面 / 战斗中 / 已登录 / 截图就绪"等布尔状态，避免每个任务对同一画面重复识别。状态由各任务在识别后回写。
- `src/scene/ScreenPosition.py`：按当前屏幕宽高生成 `Box` 的工具类，提供固定位置（`top_left` 等）与百分比区域。

## 6. 视觉体系

| 手段 | 入口 | 适用场景 |
| --- | --- | --- |
| 模板匹配 | ok-script 的 `wait_click_feature`、`box_of_screen` 等 | 固定 UI 元素：按钮、图标、面板 |
| SIFT 特征匹配 | `VisionMixin.find_sift_feature()` | 模板与画面存在缩放/小目标差异的场景 |
| 旋转匹配 | `VisionMixin.find_rotated_template()` | 带旋转的目标（如小地图箭头） |
| OCR | ok-script OCR（onnxocr + OpenVINO） | 文字内容：数量、等级、体力 |
| 目标检测 | `BaseNTETask.openvino_detect()` | 血条、敌人、元素等语义目标 |

要点：

- `src/Labels.py` 是全部模板特征的枚举，由框架根据 `ok_templates/` 与 `assets/coco_annotations.json` 自动生成，**不要手工编辑**。
- `ok_templates/` 是 git 子模块（`ok-neverness-to-everness-coco-labeling`），模板截图与标注坐标都在那里维护。
- OpenVINO 模型是全局单例（`og.my_app`），检测一律走 `openvino_detect()`，不要自行加载模型或并发清理缓存。
- 图像预处理函数放 `src/utils/game_filters.py` 或 `src/utils/image_utils.py`，避免各任务复制粘贴。

## 7. 交互层

`src/interaction/NTEInteraction.py` 继承 ok-script 的 `PostMessageInteraction`，负责后台窗口消息输入：

- `send_key()` / 鼠标点击都持有输入锁，串行化执行，避免任务与触发任务同时发指令。
- `src/interaction/cursor_sync.py`：后台运行时游戏会把光标移到屏幕中心，该组件在指令执行后自动恢复用户鼠标位置（可由全局配置关闭）。
- `src/interaction/keyboard_layout.py`：QWERTY 物理键位映射，兼容非美式键盘布局。

输入节奏保持保守：不用不可中断的高频点击与长时间按键，考虑用户真实鼠标的干扰。

## 8. 战斗系统总览

详细设计见 [Combat Planner 开发指南](combat-planner.md)，这里只给参与战斗相关开发的最低限度认知：

- `src/combat/CombatCheck.py`：入战/脱战状态判定，影响所有自动战斗入口，改动必须保守并补测试。
- `src/combat/BaseCombatTask.py`：战斗任务骨架，管理战斗会话与当前角色。
- `src/combat/planner/`：动作规划器。角色通过 `combat_plan()` **声明式**地 yield `ActionIntent` 动作与入场诉求，planner 统一打分、裁决切人与协作请求；创建 plan 时不得发送输入或产生副作用。
- `src/char/BaseChar.py`：角色基类（元素枚举、技能释放、动作 helper）。
- `src/char/*.py`：内置角色实现，每个角色一个文件；`src/char/core/CharRegistry.py` 自动扫描该目录发现角色类，无需手工注册。
- `src/char/custom/`：自定义角色与出招表数据库（含迁移逻辑）；`src/char/workshop/`：角色中心资产管理。
- 修改 planner 公开 API 时，同步更新 `docs/combat_planner.md` 与 `tests/TestCombatPlanner.py`。

## 9. 声音触发

`src/sound_trigger/` 用"听"代替"看"实现自动闪避与反击：

- `capture/`：WASAPI loopback 捕获游戏进程的系统音频输出，纯系统音频层读取，不做进程注入或 Hook。结构体布局与 HRESULT 有单测 `tests/test_sound_trigger_capture.py`。
- `SoundListener.py`：librosa + scipy 对音频做滤波与相关性分析，与攻击音效模板比对。
- `DodgeCounterTrigger.py` / `SoundCombatContext.py`：达到阈值后触发闪避/反击。

音频线程失败必须可重启或清晰停止，不让异常静默杀死声音触发；没有样本文件时快速失败。

## 10. 模块地图速查

| 路径 | 职责 |
| --- | --- |
| `src/tasks/BaseNTETask.py` | 任务公共基类，组装全部 mixin 能力 |
| `src/tasks/NTEOneTimeTask.py` | 一次性任务前置检查 |
| `src/tasks/daily/` | 日常任务（一键日常、领奖、礼物、咖啡、喷泉、家具、影院约会） |
| `src/tasks/trigger/` | 触发任务（自动战斗、声音触发、跳过对话、快速传送、粉爪便利、自动登录） |
| `src/tasks/mixin/` | 跨任务公共能力（视觉、移动、角色 UI、场景流转、轮次、前后台） |
| `src/tasks/*.py` | 独立功能任务（钓鱼、音游、异象、粉爪、拍卖、排球、呗果等） |
| `src/tasks/flow/scene_flow.py` | 场景流转框架，见 SceneFlow 作者指南 |
| `src/combat/` | 战斗状态判定与战斗任务骨架 |
| `src/combat/planner/` | 动作规划器（types/context/requests/core/state） |
| `src/char/` | 角色基类、内置角色、注册工厂、自定义角色、workshop |
| `src/ui/` | 自定义标签页与 UI 基础组件（foundation/、features/） |
| `src/sound_trigger/` | 音频捕获与闪避/反击触发 |
| `src/interaction/` | 后台键鼠输入、光标同步、键盘布局 |
| `src/scene/` | 场景状态缓存与屏幕位置抽象 |
| `src/vision/` | OpenVINO 目标检测器封装 |
| `src/utils/` | 图像处理、游戏滤镜、OCR 辅助、日志限流、模板缓存 |
| `src/gifts/` `src/coffee/` | 赠礼与咖啡子系统 |
| `src/midi_player/` | MIDI 解析、音轨分析、琴键布局、演奏控制 |
| `src/heist_path/` | 粉爪大劫案路线脚本 |
| `src/audio/routing.py` | 后台音频路由配置项 |
| `src/patches/` | 启动补丁（i18n、任务标签页兼容） |
| `src/globals.py` | 全局单例（模型等重资源），经 `og.my_app` 访问 |
| `src/Labels.py` | 模板特征枚举（自动生成） |
| `ok_templates/` | git 子模块，模板图与 COCO 标注来源 |
| `tests/` | unittest 测试套件 |

## 11. 常见开发任务

### 11.1 新增一次性任务

1. 在 `src/tasks/` 下新建文件（日常类放 `src/tasks/daily/`）：

```python
from src.Labels import Labels
from src.tasks.BaseNTETask import BaseNTETask
from src.tasks.NTEOneTimeTask import NTEOneTimeTask


class MyTask(NTEOneTimeTask, BaseNTETask):
    CONF_MY_OPTION = "我的选项"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "我的任务"  # 界面显示名
        self.description = "任务的一句话说明"
        self.default_config.update({self.CONF_MY_OPTION: True})
        self.config_description = {self.CONF_MY_OPTION: "选项的界面说明"}

    def run(self):
        super().run()  # 必须先调用前置检查
        self.ensure_main()  # 确保回到游戏主界面
        panel = self.wait_panel(Labels.mail_panel, time_out=5)
        if panel is None:
            self.log_error("未找到目标面板, 任务中止")
            return
        self.operate_click(0.87, 0.87)  # 相对屏幕比例点击
        self.send_key("esc")
```

2. 在 `src/config.py` 的 `onetime_tasks` 中注册：

```python
["src.tasks.MyTask", "MyTask"],
```

3. 同步 i18n 翻译；涉及新模板时先完成模板采集（见 11.4）。

### 11.2 新增触发任务

```python
from ok import TriggerTask

from src.tasks.BaseNTETask import BaseNTETask


class MyTriggerTask(BaseNTETask, TriggerTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "我的触发"
        self.trigger_interval = 1.0  # 循环间隔, 保持保守
        self.default_config = {"_enabled": False}  # 默认关闭

    def run(self):
        if not self.in_team_and_world():  # 轻量检查, 不满足立即返回
            return
        ...
```

在 `trigger_tasks` 中注册。触发循环必须轻量、可中断；识别不到目标时尽快返回而不是长时间阻塞。

### 11.3 新增角色

内置角色：在 `src/char/` 下新建文件，定义 `BaseChar` 子类即可，`CharRegistry` 会自动扫描发现：

```python
from src.char.BaseChar import BaseChar


class MyChar(BaseChar):
    cn_name = "角色中文名"
    element = BaseChar.ElementType.BLUE

    def combat_plan(self, context):
        # 声明动作与入场诉求, 创建 plan 时不得发送输入
        ...
```

要点：优先覆盖 `describe_role()`、`combat_plan()`、`combat_policies()` 与小型动作 helper；动作以 `ActionIntent` 声明（`tags`、`execute`、`slot` 等字段见 planner 指南）；协作请求走 planner 公开 API，不读取 `CombatContext` 内部字段，不依赖下划线开头的内部成员。新增角色后为识别与战斗行为补测试（参考 `tests/TestChar.py`、`tests/TestCombatPlanner.py`）。

外置角色（用户目录加载、同目录导入）见 [贡献与验证](contributing.md) 中的"外置角色的同目录导入"。

### 11.4 新增模板特征

1. 模板截图与 COCO 标注在 `ok_templates/` 子模块仓库中维护，更新后同步子模块引用；同时检查 `assets/coco_annotations.json` 与相关测试。
2. 重新生成或更新 `src/Labels.py` 枚举（框架 `template_tab.generate_label_enum` 自动生成，人工只做审查）。
3. 模板图放 `ok_templates/`，命名跨 Windows 大小写/分隔符保持稳定，避免无意义大文件。

### 11.5 新增 UI 标签页

1. 在 `src/ui/` 新建标签页类，复用 `src/ui/foundation/` 的组件与 ok-script/qfluentwidgets 模式，不引入新 UI 框架。
2. 在 `src/config.py` 的 `custom_tabs` 注册：`["src.ui.MyTab", "MyTab"]`。
3. UI 线程只做展示与轻量状态协调；长耗时 OCR、模型推理、音频捕获、文件扫描一律放后台线程。

### 11.6 新增全局配置项

在 `src/config.py` 定义 `ConfigOption` 并加入 `global_configs`：

```python
my_option = ConfigOption(
    "My Option",
    {"Enable": False},  # 实验能力默认关闭
    description="What it does",
    config_description={"Enable": "Detailed description and risk note"},
)
```

风险较高的实验能力必须默认关闭，文案说明风险，并保留现有方案作为默认路径。

## 12. 关键 API 速查

以下方法均可在 `BaseNTETask` 及其 mixin 中直接调用：

| 方法 | 用途 |
| --- | --- |
| `click(x, y, move_back, name)` / `operate_click(x, y, ...)` | 点击（支持坐标、`Box`、比例） |
| `send_key(key, down_time)` | 发送按键 |
| `scroll(x, y, count)` | 滚动 |
| `move_mouse_relative(dx, dy)` | 相对移动鼠标 |
| `wait_until(condition, ...)` | 等待条件成立（SceneFlowMixin 增强） |
| `wait_panel(feature, box, threshold, time_out)` | 等待某面板特征出现 |
| `find_sift_feature(feature_name, box, ...)` | SIFT 查找特征，返回 `Box` 或 `None` |
| `openvino_detect(...)` | OpenVINO 目标检测（复用全局模型） |
| `ensure_main(esc, in_world, time_out)` | 回到游戏主界面 |
| `is_main()` / `in_team_and_world()` / `wait_in_team_and_world()` | 场景状态判断 |
| `openF1panel()` / `openF2panel()` / `openF5panel()` / `openESCpanel()` | 打开常用面板 |
| `walk_until_interac(direction, time_out)` | 行走直到出现交互按钮 |
| `click_nearest_map_teleport(box)` / `click_traval_button()` | 地图传送 |
| `start_rounds()` / `begin_round()` / `add_success()` / `finish_rounds()` | 多轮次控制 |
| `get_current_char_index()` | 当前操控角色索引 |
| `bring_to_front()` / `is_foreground()` | 前后台切换 |
| `log_info()` / `log_warning()` / `log_error()` | 日志（不得含用户隐私） |

## 13. 测试

- 全量测试：`.\.venv\Scripts\python.exe -m unittest discover -s tests -p "*.py"`
- 单文件：`.\.venv\Scripts\python.exe -m unittest tests.TestCombatPlanner`
- 逐文件脚本：`.\run_tests.ps1`
- 语法检查：`.\.venv\Scripts\python.exe -m py_compile path\to\file.py`；安装了 ruff 时运行 `ruff check .`

测试策略：planner、纯逻辑解析、数据迁移、配置转换、音频辅助函数优先写 unittest；不为 UI 拼装或胶水代码机械加测试；图像识别难以单测时隔离纯计算部分，并在 PR 说明人工验证范围。需要 Windows 真实环境的测试用 `skipUnless` 标记。修复 bug 时尽量添加回归测试。

## 14. 编码规范要点

完整规范见根目录 `AGENTS.md`，最常见的几条：

- Python 3.12+；遵循 `pyproject.toml` 的 ruff 配置（行宽 100、双引号、导入排序）。
- Python 源码内的注释与字符串使用 ASCII `,` `;`，不用全角标点。
- 生产代码禁止裸 `except` 与静默失败；捕获异常时记录上下文并保持可恢复或明确中止。
- 线程、事件、后台循环必须有停止条件；不把重模型、音频循环、OCR 大扫描放进 UI 热路径。
- 日志不含账号、完整本机路径、隐私截图内容；`configs/`、用户截图、日志不入库。
- 不为小功能引入大型框架、服务端组件或异步运行时。

## 15. 调试建议

- 使用 `main_debug.py` 启动获取详细日志；`logs/` 与 `screenshots/` 仅本地留存，不提交。
- 复现识别问题时，先检查分辨率/比例（16:9、1080p+）、UI 透明度、显卡滤镜与 HDR 设置——这些是历史问题的高频来源。
- 任务卡住时优先检查：场景判断是否提前返回、面板是否真的打开（`wait_panel` 超时）、点击坐标是否落在预期 `Box` 内。
- 涉及战斗、声音触发、后台输入的改动，除单测外需在真实环境人工验证并在 PR 中说明验证范围。
