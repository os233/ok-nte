# ok-nte 单元测试约束

本文件是 `tests/` 的强制约定, 与根目录 `AGENTS.md` 的「测试策略」一节配套。
`AGENTS.md` 说明**该测什么**, 本文件说明**怎么写才算合格的测试**。

新增或修改测试前先读本文; 评审测试代码时按本文逐条对照。

---

## 0. 适用范围与运行方式

解释器一律用仓库虚拟环境, 不用全局 `python` / `pytest`:

```powershell
# 全量 (逐文件独立进程, 避免 Qt / 单例互相污染)
.\run_tests.ps1
# 全量 (单进程 discover)
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "*.py"
# 定向 (改哪个模块跑哪个, 这是日常主力)
.\.venv\Scripts\python.exe -m unittest tests.TestAutoBidAuctionTask
```

沙箱 / CI 下用 `python` 命令但工作目录不同时, 记得带 `--top-level-directory .`,
否则 `tests` 包内 `from src...` 的绝对导入会失败。

---

## 1. 硬性约束 (违反即视为测试不合格)

| # | 约束 | 原因 |
|---|---|---|
| C1 | 测试不得依赖真实游戏窗口、真实截图、真实音频设备、真实网络 | 否则开发机与 CI 结果不一致, 且失败原因不可复现 |
| C2 | 测试不得写入仓库内会被提交的路径 | 测试产物必须落在 `tempfile.TemporaryDirectory()` 或 `tests/.tmp/<uuid>/`, 并在 `tearDown` 清理 |
| C3 | 不得断言日志的**精确文案** | 文案是易变实现细节。只有日志被外部流程当稳定契约消费时才测内容 |
| C4 | 不得断言私有方法的**调用顺序**, 只断言状态变化 / 返回值 / 用户可见结果 | 顺序断言把重构锁死, 但拦不住真实 bug |
| C5 | 不得修改被测行为去迎合测试 | 测试是行为的锁, 不是行为的定义 |
| C6 | 被测模块 import 产生副作用时 (如回写 `configs/*.json`), 必须在测试内隔离或接受并说明 | 见 §5 |
| C7 | 每个新增的**失败分支 / 异常恢复路径**至少一条测试 | 这类分支最容易静默失效, 也最缺覆盖 |
| C8 | 测试必须能在全量 `discover` 下通过, 不允许「单跑过、全跑挂」 | 见 §7 隔离要求 |

---

## 2. 测试分层: 先判类型再动手

按被测对象的**依赖强度**选写法, 不要一律 `Mock` 到底。

### L1 纯逻辑 (优先, 成本最低)

解析、数据迁移、配置映射、状态机判定、几何/数值计算。
**不实例化任务, 不 mock 任何东西**, 直接调函数。

```python
# tests/TestCoffeeTask.py - TestCoffeeRuntime
class TestCoffeeRuntime(unittest.TestCase):
    def test_price_re_matches_supported_ocr_formats(self):
        for text, expected in (("1,234", 1234), ("￥5000", 5000)):
            with self.subTest(text=text):
                self.assertEqual(parse_price(text), expected)
```

判定标准: 如果这条逻辑能被抽成纯函数, 那么**先抽出来再测**, 不要靠 mock 任务来间接覆盖。

### L2 任务 / 业务流程 (主力形态)

绕开框架初始化, 只保留被测方法需要的依赖。参照 `tests/TestAutoBidAuctionTask.py`:

```python
def _make_task(config: dict | None = None) -> XxxTask:
    """构造跳过 ok 框架初始化的任务实例, 只保留被测方法需要的依赖。"""
    task = XxxTask.__new__(XxxTask)
    task.log_info = Mock(); task.log_warning = Mock(); task.log_error = Mock()
    task.sleep = Mock()
    task.config = _config(**(config or {}))
    return task
```

纪律:

- **只在 IO 边界打桩**: OCR、点击、截图、文件读取、时间。**控制流必须是真实的**。
- 走真实 `__init__` 只在测「初始化装配」时才需要, 且要 `patch.object(BaseNTETask, "__init__", return_value=None)`
  —— 真实基类构造会拉起 UI / 单例。
- 桩 OCR 这类带关键字参数的接口, 形参写 `*args, **kw`。框架会传 `time_out=`,
  写死形参会 `TypeError` —— 那不是被测代码的错。
- 配置对象用「只认识给定键」的桩, 未给出的键返回调用方默认值; 不要构造真实 `Config`。

### L3 UI / 需要 Qt 的模块

`setUpClass` 里建 `QApplication.instance() or QApplication([])`, 结束时 `deleteLater()`。
Qt 对象必须显式释放, 否则跨测试互相残留。这类测试**不计入 CI 必过项**,
在文件头 docstring 写明人工验证范围。

### L4 子进程 / 真实环境

用于框架级行为验证 (如日志门控、CLI 输出), 参照 `tests/test_log_gate.py`。
必须有超时, 且不得依赖用户环境中的绝对路径。

---

## 3. 时间: 必须可控

任何带超时 / 重试 / 节流的代码, 测试里**不允许真实等待**。

- 任务内 `sleep` 换成推进假时钟的 `Mock`。
- 模块级 `time.monotonic()` 的 deadline 计算, 必须 `patch.object(被测模块, "time", fake_clock)`
  —— 只换 `task.sleep` 不够, `time.monotonic()` 仍返回真实时间, deadline 立刻过期,
  测试会误报「第一轮就超时」。
- `_FakeTime` 同时提供 `monotonic()` 和 `sleep()`, 写法见 `tests/TestAutoBidAuctionTask.py`。
- 断言的是「第几次轮询发生某事」而不是「实际耗时多少秒」。

---

## 4. 断言要有判别力 (本文件最重要的一条)

**「测试通过」不构成证据** —— 通过可能是因为根本没有测试覆盖那条分支。

### 4.1 最低要求: 每条修复配一次变异验证

修完 bug 后, 把修复**改回缺陷**, 跑配对的测试, 确认它**失败**。
具体流程与 5 类「假捕获」陷阱见 skill `logic-bug-audit-mutation-verify` §5。

### 4.2 陷阱: 两个约束在数值上恰好相等

若用时间断言上限, 而代码里同时存在「次数上限」和「时间上限」, 且
`次数 × 轮询间隔 == 时间上限`, 那么无论哪个上限生效耗时都一样, 断言没有判别力。
对策: 让被测量的两个来源**数值分开** (如把循环体耗时设成 ≠ 轮询间隔), 一次就能区分。

### 4.3 陷阱: 只断言选项, 不断言默认值

新增常量 / 枚举 / 配置项时, 除了断言取值集合, 必须补一条断言**默认值**的用例。
否则默认值被改回旧值也拦不住。

### 4.4 优先级

判别力问题先分清两种「漏过」:

1. **真的没覆盖** → 补断言 (首选)。
2. **断言没有判别力** → 改断言的口径 (测真正区分两个版本的那个量)。

等价变异确实不重要时才删除, 并在提交信息里说明理由。**不要把「漏过」当成可接受的通过率。**

---

## 5. 副作用与状态污染

- **import 被测模块可能产生副作用。** 本项目已知: `import src.tasks.*` 会触发
  `Config.verify_config`, 补缺失键并丢弃旧键后**回写 `configs/*.json`**。
  测试里要么在隔离目录下运行, 要么接受并确保只增删键、不覆盖用户已设的值。
- **单例 / 全局状态必须在 `tearDown` 复位**, 典型清单:
  `CustomCharManager._instance = None`、`CustomCharDb.reset_instance()`、
  `char_registry.rescan_external()`, 以及所有 `patch` 的 `patcher.stop()`。
- 多个 `patch` 用列表统一收集, `tearDown` 里循环 stop, 不要散落成 `with` 嵌套。
- 写文件操作全程用 `read_bytes()` / `write_bytes()` —— 仓库是纯 CRLF,
  `read_text()` / `write_text()` 往返会把整个文件换成 LF。

---

## 6. 命名与结构

- 测试方法名描述**行为与预期**, 不描述实现:
  `test_claim_button_missing_still_closes_dialog` ✅ / `test_call_close_2` ❌
- 每条用例的 docstring 写「**为什么这条行为重要**」, 尤其是那些「漏调不会让任何流程失败」
  的路径 (告警、清理、通知)。这类断言最容易被后人当冗余删掉。
- 一个类聚焦一个被测单元; 用 `subTest` 覆盖同一逻辑的多组输入, 不要复制粘贴用例。
- 测试数据就地构造, 不依赖其他测试的执行顺序, 不共享可变类属性。
- 共享的构造 helper 用模块级 `_make_xxx()` 函数, 不用 import 自其他测试文件的 helper。

---

## 7. 失败归类: 环境性失败不等于回归

全量 `discover` 存在**环境性失败** (需要真实设备 / 截图 / 音频, 典型表现是
`'NoneType' object has no attribute 'set_images'`), 集中在 `TestChar` /
`TestGiftManager` / `TestTeamManager*` / `TestWorkshop`。

判定方法: `grep -rl "<被审模块名>" tests/` —— 若失败文件里没有一个引用被审模块,
判为环境性失败。

汇报时**必须分开写**:

```
定向测试: tests.TestXxx 42 passed
全量 discover: 12 个环境性失败 (无一动到本模块), 详见 …
```

不要含糊成「测试基本通过」。

---

## 8. 提交前核查清单

- [ ] 跑过定向测试, 拿到明确的 `Ran N tests / OK`
- [ ] 每条修复做过**变异验证**, 确认配对的测试会失败 (附捕获 / 漏网 / 无效三格计数)
- [ ] 新增常量 / 枚举 / 配置项时, 补了默认值断言
- [ ] 测试无真实等待、无网络、无真实设备依赖
- [ ] 临时文件落在临时目录且已清理
- [ ] 全局单例与 patch 已在 `tearDown` 复位
- [ ] `ruff check tests/<file>.py` 无新增报错 (全仓有既有报错, 只看自己改的文件)
- [ ] 单独跑该测试文件通过, 全量 discover 无新增失败
- [ ] 如果某条行为**无法**自动化验证, 在最终回复里写明「未验证 + 原因 + 人工验证范围」
