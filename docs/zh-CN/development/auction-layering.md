# 拍卖任务分层设计（提案）

> 状态: **提案, 未实施**。实施完成后把本文并进 `mkdocs.yml` 的 nav。
> 依据的代码状态: 分支 `fix/auction-2` @ `0e8e93d` + 工作区未提交改动
> (`AutoBidAuctionTask.py` 3238 行 / `TestAutoBidAuctionTask.py` 282 条用例全绿)。

## 0. 修正日志

写这份提案的过程里, 我有 **5 条论断被实测推翻, 1 条被实测升级**。全部记在这里,
因为修正过程比结论更有用 —— 它说明「文件 3238 行所以该拆」这类直觉判断有多容易错。

| # | 曾经的论断 | 实测结果 | 证据 |
|---|---|---|---|
| 1 | `default_config` 展开后 50 个条目 | **21 个** | 装配后 `len(default_config)` = 21 |
| 2 | 纯函数「在当前结构下没法单独测」 | **错, 已能单测** | 4 个函数全是 `@staticmethod`; 测试里已有 `AutoBidAuctionTask._is_partial_number_text(",544")` 这类直接调用 |
| 3 | 配置声明层「0 测试覆盖」 | **错, 覆盖充分** | `TestAuctionConfigDescriptions` / `...BidModeConfigVisibility` / `...SellModeConfigVisibility` / `...AssistFeaturesConfig` / `...RaiseModeConfig` 共 5 个类 |
| 4 | `CONF_ASSIST_FEATURES`「不在任何可达性用例里」 | **错** | `test_assist_features_render_as_a_multi_selection` 断言它出现在 `build_config_fields` 的输出里 |
| 5 | 出售的两条不对称规则「没有任何用例钉住」 | **错, 15 条用例** | `TestAuctionSellFailureEscalation` 覆盖了「非满仓失败不累加」「达阈值以放宽集合开局」「超时不计入」「成功清零」 |
| 6 | 全角逗号问题「只是一条没写用例的已知缺陷」 | **升级为已复现的真实缺陷** | 5 对半角/全角输入实测: 半角侧 5 个全部命中防线, 全角侧 5 个**全部漏过**(见 §3 缺口 1) |

错误 3 和 5 是我自己的取证失误, 成因不同, 都值得记:

- **错误 3**: 检查命令里 `grep -c` 返回 0 匹配时退出码为 1, 被 `&&` 链截断,
  后面那句 grep 根本没执行 —— 我却把「没输出」当成了「没覆盖」。
  → **取证命令不要用 `&&` 串。**
- **错误 5**: 我只看了生产代码的 docstring, 没去读对应测试类就下了「没有用例」的结论。
  → **说「没有测试」之前必须先列出该测试类的方法名。**

**修正后的结论**: 这个任务不是「有纪律但单文件过大」, 而是**比预期好得多** ——
配置声明层、出售升级规则、纯函数都已有针对性覆盖。分层的理由因此从「必要」降为「可选」;
而真正该先做的是一件完全不同的事: 修 §3 缺口 1 那个全角逗号缺陷(1 行)。
详见 §4。

---

## 1. 结论

**直接回答「需不需要分层」: 现在不需要。**

实测下来, 这个任务的配置声明层、出售升级规则、纯函数都已有针对性测试覆盖,
分层的收益只是「降低新增功能时的搜索成本」, 属可维护性优化, 不修任何缺陷。
而同一轮实测里找到了一个**已复现、可 1 行修掉的真实缺陷**(§3 缺口 1)。
先把那个修掉, 比拆文件有价值得多。

**直接回答「要不要按 GUI / 坐标 / 逻辑 三分」: 不要**, 三分法有两个具体错误。

- **坐标层切错。** 拍卖的 38 个区域里, 只有 2~3 个是「面板上的点击坐标」, 其余 35 个是
  **OCR 裁框**。项目约定是「位置表只收面板点击坐标, 不收 OCR 区域」(`src/scene/PanelPosition.py`
  只放 `ScreenRatio` 点值)。把 35 个裁框塞进 `PositionMap` 会把 OCR 区域与全局场景位置混在一起,
  而且拍卖的裁框带大量标定注释(见 `BOX_ESTIMATE` 的 8 行注释), 塞进去只会污染那个 46 行的文件。
- **逻辑层切得太大。** 「逻辑」是 93 个方法 / 2560 行, 里面混着四种依赖强度完全不同的东西。
  整块搬到 `AuctionLogic.py` 只是把 3238 行的上帝类变成 2560 行的上帝模块, 依赖方向没有变。

正确的切法是 **四层, 按依赖方向**:

| 层 | 职责 | 依赖 | 目标位置 |
|---|---|---|---|
| **L1 声明** | 配置键的默认值 / 说明 / 面板可见性 / 任务卡「说明」 | 无 | `src/tasks/auction/options.py` |
| **L2 契约** | 38 个区域框 + 22 条匹配串 + `AuctionBoxes` 构建 | 无 | `src/tasks/auction/layout.py` |
| **L3 能力** | 领域子流程(出售 / 低保 / 界面判定 / 回场) | 只依赖 L2 + 框架原语 | `src/tasks/auction/*.py` |
| **L4 编排** | 轮次状态机 / 收尾 | L1+L2+L3 | **留在 `AutoBidAuctionTask.py`** |

L4 必须留在任务类。它是「把上面所有东西串起来」的地方, 搬走不会让任何一处变简单,
只会让入口更难找。

---

## 2. 现状数据

| 项 | 数量 |
|---|---|
| `AutoBidAuctionTask.py` | 3238 行(全仓最大, 第二名 `src/coffee/runtime.py` 1811 行) |
| 类内方法 | 93 个 / 方法体 2560 行 |
| 类常量区 (L234-563) | 330 行 |
| `CONF_*` 常量 / `default_config` 实际键 | 16 个 / 21 个 |
| `BOX_*` / `AuctionBoxes` 字段 / `RE_*` | 38 / 35 / 22 |
| `__init__` | 171 行(其中 GUI 声明 135 行, 运行时状态初始化 14 行) |
| 任务卡说明 `INST` | 85 行 |
| `TestAutoBidAuctionTask.py` | 3877 行 / 44 个测试类 / 282 条 |

**既有优势(这才是分层的真正起点)**:

- 坐标已经是**注入式**的 —— 方法签名统一带 `boxes: AuctionBoxes`, 50 行代码用 `boxes.xxx`
  而不是 `self.BOX_xxx`。所以 L2 不是「从零建」, 而是「把定义搬出任务类」。
- 纯函数已经是 `@staticmethod` 且已直接单测。
- 配置声明层有 5 个测试类守着描述完整性与可达性。
- 出售升级规则有 15 条用例, 包括两条反直觉规则。

---

## 3. 真实缺口(穷举核对后只剩三条)

三条都实测过。前两条是**可以立刻补的小洞**, 第三条是**唯一值得讨论的行为问题**。

### 缺口 1: 全角逗号让两条残缺读数防线同时失效 (已复现, 可 1 行修掉)

`_parse_asset_value` / `_is_partial_number_text` / `_has_inconsistent_grouping` 三个函数
都有同一行:

```python
normalized = raw_text.translate(FULLWIDTH_DIGITS)      # 只转全角数字 ０-９
digits_and_commas = re.sub(r"[^\d,]", "", normalized)  # 全角逗号 ，在这里被当普通字符删掉
```

`FULLWIDTH_DIGITS`(L55)只覆盖全角**数字**, 不含全角**逗号**。所以 `，` 被 `[^\d,]` 删掉,
两条防线的输入被悄悄改写成「没有逗号的正常数字」。实测对照:

| 半角输入 | partial | inconsistent | 全角输入 | partial | inconsistent |
|---|---|---|---|---|---|
| `,643` | **True** ✓ | — | `，643` | **False** ✗ | — |
| `：,523` | **True** ✓ | — | `：，523` | **False** ✗ | — |
| `,1234` | **True** ✓ | — | `，1234` | **False** ✗ | — |
| `1,23,456` | — | **True** ✓ | `1，23，456` | — | **False** ✗ |
| `12,34` | — | **True** ✓ | `12，34` | — | **False** ✗ |

10 个输入里, 半角侧 5 个全部命中防线, 全角侧 5 个**全部漏过**。

**影响**: 两个函数是 `_read_estimate_value`(L2106/2109)和 `_read_asset_value`(L2141)
的残缺读数防线。防线失效意味着 OCR 把估价读成 `，643` 这种截断值时会被当成 643 接受 ——
`_is_partial_number_text` 的 docstring 写得很清楚: 「把它当结果会按低一个数量级的价格出价」。

**触发条件窄**: 需要 OCR 输出全角逗号。作者已经防了全角数字(`FULLWIDTH_DIGITS` 就是为此存在),
说明这个风险类别是被承认的, 全角逗号是同一类里漏掉的一个。

**当前零覆盖**: `grep -c "_parse_asset_value" tests/` = **0**; 测试里没有任何全角数字或
全角逗号的数值用例(唯一的全角出现在 `test_descriptions_use_ascii_punctuation`, 守的是文案标点)。

**修法(1 行)**: 把全角逗号加进归一化。`_parse_asset_value` 本来就会 `re.sub(r"[^\d]", "")`
把所有逗号去掉, 所以这一改**不影响它的结果**, 只让两条防线看到真实的文本形态。
最小改法是扩表并改名(表名要跟着语义走, 别让它变成谎话):

```python
# 全角数字与全角逗号统一转半角: 逗号必须一起转, 否则 _is_partial_number_text /
# _has_inconsistent_grouping 的 re.sub(r"[^\d,]", "") 会把「，643」删成「643」,
# 两条残缺读数防线同时失效。
FULLWIDTH_NUMERIC = str.maketrans("０１２３４５６７８９，", "0123456789,")
```

`FULLWIDTH_DIGITS` 的其余用途(L2001 / L2020 / L2035 是上面三个函数, L2742 是
`_read_welfare_counter`)一并改名即可, 行为只在 L2020 / L2035 两处变化。

### 缺口 2: 配置可达性用例是手工列举的, 新键不会自动纳入

两条可达性用例都靠**手写键集合**:

- `TestAuctionBidModeConfigVisibility` 里手写 `price_keys = {CONF_FIXED_PRICE, ..., *CONF_BID_PRICES}`
- `TestAuctionSellModeConfigVisibility` 里手写 `sell_keys = {CONF_SELL_INTERVAL, CONF_SELL_BEFORE_WELFARE, CONF_SELL_AFTER_WELFARE}`

**新增配置键时如果忘了加进这两个手写集合, 它就没有可达性保护。**

穷举 `default_config` 全部 21 个键、对模式组合做笛卡尔积扫描即可覆盖。实测:
**144 种组合已能覆盖全部 21 个键, 当前 0 个不可达键** —— 所以这是**防回归**, 不是找 bug。

成本: 1 条用例替换 2 条手写集合, ~30 行。

### 缺口 3: 「已达阈值 → 非满仓失败 → 满仓出售」这条序列没有用例, 且行为没有结论

现状(已由用例钉住的部分):

| 规则 | 用例 |
|---|---|
| 满仓失败累加计数, 达阈值后以放宽集合开局 | `test_reaching_the_threshold_starts_with_the_escalated_set` |
| 非满仓失败**不累加**计数 | `test_not_full_failure_does_not_feed_the_escalation_counter` |
| 非满仓失败**不清零**计数 | 同上(断言 `_sell_failures` 保持 `阈值-1`) |
| 超时不计入、不置 `_inventory_stuck` | `test_sell_timeout_does_not_feed_the_escalation_counter` |
| 成功清零 | `test_success_resets_the_failure_counter` |

**没有用例的那条序列**: 计数已达阈值 → 中间发生若干次**非满仓失败**(计数不变)
→ 下一次满仓出售**仍以放宽集合开局**。

`_sell_collections_with_escalation` 的 23 行 docstring(3036-3058)解释了
「非满仓为什么不该累加」, 但**没有解释「非满仓为什么不该清零」**。
后果是: 几个小时前两次满仓失败把计数推到阈值, 之后一直是非满仓抖动, 计数一直挂着,
下一次满仓出售直接 6 个品质全卖 —— 包括用户明确保留的。

**影响有界, 所以优先级低**: 计数只在**满仓**失败时累加, 满仓本身是低频事件;
且计数达阈值本来就意味着「这个仓库已经卖不动了」。真正会踩到的场景是
「满仓失败 → 仓库被手动清空 → 很久后再次满仓」。

这是**行为决策问题, 不是结构问题**。要么判定它合理并补一条用例钉住,
要么判定它不合理并修掉。两条路都只需改几行。

---

## 4. 三个方案

### 方案 A — 修全角逗号 + 补可达性守卫 (推荐先做)

| 项 | 内容 |
|---|---|
| 改什么 | 缺口 1: 扩 `FULLWIDTH_DIGITS` 表加入全角逗号(1 行) + 5 对半角/全角对照用例(先红后绿)。缺口 2: 把两条手写可达性用例换成穷举扫描(1 条)。缺口 3: 补一条用例把当前行为钉住(不改行为) |
| 生产代码 | **1 行**(L55 的 `str.maketrans` 加一个字符 + 改名 + 注释) |
| diff | ~1 行生产 + ~80 行测试 |
| 风险 | 低(改动点只有常量表; `_parse_asset_value` 的结果不变, 已在 §3 论证) |
| 收益 | **唯一一个既有复现证据、又能 1 行修掉的缺陷**; 顺带补上新增配置键的可达性保护 |
| 验证 | 5 对用例先跑出红(证明缺陷真实存在), 改完转绿; 定向测试 282 + 新增全绿 |

**为什么推荐先做**: 它同时满足「有复现证据」「改动最小」「有回归用例」三条,
而且**不依赖任何分层工作**。如果这一轮只做一件事, 做这个。

### 方案 B — 修缺口 3 的行为 (可选, 与 A 独立)

| 项 | 内容 |
|---|---|
| 前置 | 方案 A 里那条「钉住现状」的用例必须先写。它是修复前的红/绿对照 |
| 改什么 | 非满仓失败时把 `_sell_failures` 清零(而非保持不变), 并在 docstring 补上理由 |
| 生产代码 | 改 1 行(`if not inventory_full:` 分支加一句 `self._sell_failures = 0`) |
| diff | ~1 行生产 + ~10 行测试 |
| 风险 | 低(改动局部, 且已有 15 条用例在周围守着) |
| 收益 | 不会因为几小时前的一次满仓失败而过早卖掉用户保留的品质(影响有界, 见 §3 缺口 3) |
| 反面理由 | 「满仓失败是连续失败才有意义」这个口径下, 非满仓失败**中断**了连续性, 清零更符合语义; 但清零也会让「满仓 → 抖动 → 满仓」这种真实连续场景丢掉计数。**这是个判断, 需要用户拍板** |

### 方案 C — 完整四层拆包 (前两步稳定后再评估)

即 §5 的路径。**收益是纯可维护性, 不修任何缺陷**, 且代价是热重载失效(§8)。

| 阶段 | 搬什么 | 风险 | 文件行数变化 |
|---|---|---|---|
| 1 | L1 声明层 → `auction/options.py` | 低 | −225 行 |
| 2 | L2 契约层 → `auction/layout.py` | 低 | −170 行 |
| 3 | L3 领域层 → `auction/{sell,welfare,screens}.py`(mixin) | 中 | −700 行 |
| 4 | 常量按「跟着用它的代码走」重排 + 依赖方向用例 | 低 | — |

完成后 `AutoBidAuctionTask.py` 约 600~700 行, 最长模块约 405 行(`sell.py`)。

### 推荐顺序

```
方案 A  →  (方案 B, 需先拍板)  →  (视需要) 方案 C 阶段 1/2  →  方案 C 阶段 3
```

- A 有独立价值, 不依赖任何其他方案, 且是三者里唯一「有缺陷收益 + 改动极小」的。
- B 是一个 1 行的行为改动, 但**必须用户先决定口径**, 不能由实施者顺手定。
- C 的阶段 1/2 风险低, 收益是「新增配置项/改坐标时不用在 3238 行里找」。
- C 的阶段 3 收益最大风险最高, 单独开分支。

**回到原问题**: 「拍卖任务需不需要分层」的答案是 —— **现在不需要**。
当前最该做的是方案 A 那个 1 行的全角逗号修复; 分层是等这类实际缺陷清完之后,
再用来降低「新增功能时的搜索成本」的优化项。

---

## 5. 方案 C 的迁移路径(细化)

原则: 每阶段独立可交付, 每阶段结束时 282 条测试全绿。

### 阶段 1 — L1 声明层 (风险: 低 / 收益: 中)

**搬走**: `_inst_line` + `INST`(90 行)、`default_config` 的 26 行字典、
`config_type`/`sub_configs` 的 62 行、`config_description` 的 36 行、
第 1~6 次出价价格描述循环的 11 行 → `auction/options.py`, 导出
`build_default_config()` / `build_config_type()` / `build_config_descriptions()` / `INST`。

**留在任务类**: 16 行 `CONF_*` 键名。它们是 GUI 契约**也是测试契约**
(`tests` 里有 `task.CONF_FIXED_PRICE` 这类引用), 搬到模块级会变成模块全局量,
`task.CONF_*` 直接 `AttributeError`。16 行的重复比一次测试大改便宜。

**闸门**: 快照对拍 —— `default_config` / `config_type` / `config_description` /
`instructions` 四个结构 `json.dumps(sort_keys=True)` 后必须逐字节相等。

**收益**: 配置的「默认值 + 说明」从 2 处收敛到相邻的 1 处, 新增配置项只写一个地方。

### 阶段 2 — L2 契约层 (风险: 低 / 收益: 中)

**搬走**: 38 个 `BOX_*`、22 条 `RE_*`、`AuctionBoxes` / `PostRoundState` / `AuctionState`、
`_build_boxes` → `auction/layout.py`。

**留一个兼容别名**: `tests` 里有 `AutoBidAuctionTask.BOX_ESTIMATE`(L2384)这条引用。
在任务类里保留 `BOX_ESTIMATE = layout.BOX_ESTIMATE` 一行, 或改掉那一处引用 —— 改动量都是一行。

**闸门**: AST 归一化对比, 断言 `_build_boxes` 的 35 个 `screen(...)` 实参逐一相等。

### 阶段 3 — L3 领域层 (风险: 中 / 收益: 高)

**搬走** → `auction/sell.py`(10 个方法 405 行)、`auction/welfare.py`(~7 个方法 120 行)、
`auction/screens.py`(界面判定 + 弹窗 ~8 个方法 150 行), 各自做成 mixin, 任务类继承。

**为什么用 mixin 而不是组合**: mixin 让 `task._sell_collections` 依然可达,
**282 条测试零改动**。仓库已有 6 个 mixin 先例(`src/tasks/mixin/`), 模式一致。
组合(薄委托)会多出 ~25 个一行方法, 且每个都要维护签名。

**约束(必须写进模块 docstring)**: mixin 只通过 `self.<field>` 读任务状态,
**不得引入新的实例字段**; 新状态一律在任务类 `__init__` 里初始化, 否则测试的
单例复位路径会漏掉(`tests/TESTING.md` 的 C 系列要求)。

**闸门**: 282 条测试零改动全绿 + `task.__class__.__mro__` 含新 mixin +
变异验证(把放宽阈值改坏, 必须有用例变红)。

---

## 6. 验证闸门: 怎么证明「这只是搬移」

搬移最容易出的错是「顺手改了行为」。三个可执行的闸门:

### 闸门 A — AST 归一化对比(用于纯搬移)

对指定函数, 把源码解析成 AST 后: 去掉行号、去掉 `self.` 前缀差异、按字典序重排 import,
再 `sha256(ast.dump(...))`。搬移前后必须相等。

适用: 阶段 2 的 `_build_boxes`、阶段 3 的每个 mixin 方法。

### 闸门 B — 快照对拍(用于 L1)

`default_config` / `config_type` / `config_description` / `instructions` 四个结构
`json.dumps(sort_keys=True)` 后对比。**这条应该固化成测试**, 因为它是「配置项永久隐藏 /
说明丢失」这类静默错误的唯一防线。

### 闸门 C — 零改动全绿 + 变异验证(用于阶段 3)

```bash
./.venv/Scripts/python.exe -m unittest tests.TestAutoBidAuctionTask
# 必须: Ran 282 tests ... OK, 且 git diff 里 tests/ 无改动
```

再加一次变异: 故意改坏一处领域规则, 确认有对应用例变红(证明这些用例真的在守行为,
而不是在守实现)。

---

## 7. 明确不做的事

- **不动 `AutoBidAuctionTask` 的公开契约**: 任务名 / `group_name` / `CONF_*` 键名 /
  `default_config` 内容一律不变。配置键改名会丢用户配置。
- **不把 OCR 裁框搬进 `src/scene/`**: 见 §1 反例 A。
- **不搬纯函数**: 它们已经是 `@staticmethod` 且已可单测, 搬走零收益还丢热重载(§8)。
- **不改 282 条测试的断言语义**(方案 A/B 除外): 阶段 1~3 里 `tests/` 只允许新增。
  若某条用例因搬移而必须改, 说明搬移动了行为 —— 回去查, 不要改测试。
- **不引入抽象层**: 不加 `AuctionScreen` 基类、不加 `AuctionContext` 容器、不做依赖注入。
  目标是「按依赖方向归位」, 不是重新设计架构。
- **不顺手重构 `_run_single_round` 系列**: 轮次编排留在原地, 即使它还有改进空间。

---

## 8. 代价与取舍

| 代价 | 影响 | 处置 |
|---|---|---|
| **热重载失效** | `main_debug.py` 的文件监听只 reload **任务模块本身**: `ok/core/task_manager.py::_build_builtin_task_file_map` 按 `inspect.getfile(task.__class__)` 建表, `_on_debug_dir_changed` 只 `os.listdir` 任务目录且**跳过子目录**。拆包后改 `src/tasks/auction/*.py` 不会热重载 —— 和现在 `src/scene/*` 的情况一样 | 坐标标定本来就应该在截图离线做(见 skill `game-ui-ocr-calibrate`), 不依赖热重载; 「跑起来看效果」的场景改为改完重启。**这是方案 C 阶段 2 唯一的反对理由**, 也是「不搬纯函数」的原因 |
| 跨文件跳转 | 看一个流程要开 3~5 个文件 | `__init__.py` 重导出 + 模块 docstring 写清「本模块只放 X」 |
| 一次性大 diff | 违反 AGENTS.md「diff 小而可审查」 | 按 §5 分阶段提交, 每阶段一个 |
| 测试文件仍是 3877 行 | 本次不解决 | 已在 `tests/TESTING.md` §1.1 有删减判据, 属独立议题 |
