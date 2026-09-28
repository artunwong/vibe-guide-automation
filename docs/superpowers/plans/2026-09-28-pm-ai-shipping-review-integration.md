# pm-skills 审阅层（pm-ai-shipping）与 vibeguide 的结合方案

> 状态：**待用户确认的方案稿**，尚未实施、未改任何代码。
> 日期：2026-09-28｜vibeguide 版本：v4.9.1（main `9478145`）
> 上游：[phuryn/pm-skills](https://github.com/phuryn/pm-skills) v2.1.0，MIT，commit `8607e3b077817f89bf4a9b623246219734ac3be0`（2026-09-14）
>
> **修订 r2（2026-09-28，独立 review 后）**：r1 的落地路径建立在两个与代码不符的前提上——
> 「`vibe install` 是 skill 安装器」和「安装出来的 skill 会落到 `.vibe/proposals/skills/`」。
> 两条均已实测证伪，阶段 0 的范围与估算据此上调。逐条修订见 §7。

---

## 0. 一句话结论

pm-skills 里真正该进 vibeguide 的只有 **3 个 skill**（`pm-ai-shipping` 插件），它们补的是
vibeguide **唯一一块结构性空白：reviewer 知道按什么分级，但不知道怎么审**。

接进去的前置工作比看起来大：**skill 安装能力今天只完成了一半**——`install_skill()` 函数存在且经过
验证，但没有任何 CLI 能调用它，它写的记录也不进项目配置。所以阶段 0 不是"给现有命令加个参数"，
而是"把这条通道接通，顺带支持 monorepo 子路径"。接通之后，用两处协议文本 + 一个 AGENTS.md
分发块把方法论绑到现有的 P0–P3 判定上。**不改 reviewer 的判定逻辑，不 fork、不 vendor 上游源码。**

---

## 1. 证据基线（本方案的全部结论都出自这里）

本节所有条目为 2026-09-28 本机实测，非记忆、非上游 README 自述。

### 1.1 vibeguide 当前实现程度

| 事实 | 证据 |
|---|---|
| 代码规模 v4.9.1：`vibe_guide/` 22,508 行，`monitor.py` 5,145 行 | `wc -l vibe_guide/*.py` |
| **reviewer 的"判定与证据链"完备**：交付门校验 `in_session_review.protocol` 逐字匹配、`clearance{p0,p1,p2}` 必须是整数 0（`bool` 不算）、`evidence_ref` 非空 | `visible-sdd-worker.md:61-74`、`monitor.py:4066-4080` |
| **reviewer 的"方法论"是空的**：全项目没有任何一段文字说明"如何发现缺陷" | 见下两行 |
| 实际生效的 reviewer 路径 = **协议文本 + `monitor.py` 交付门**；`review.py`（50 行，按 `root_cause` 分桶）**全仓无生产调用点**，只被 `tests/test_v38_review_matrix.py` 引用 | `grep -rn "review\.py\|bundle_findings\|accept_review" vibe_guide/` |
| 全项目关于"怎么审"的文字**只有一句**：「按 P0（资金/数据正确性）、P1（逻辑缺陷/事务边界）、P2（边界条件/行为回归）、P3（建议）分级产出意见」 | `visible-sdd-worker.md:15`。**本项目 AGENTS.md 不含该句**（`grep -n "P0" AGENTS.md` 只命中 :77、:151 两处无关表述），该句在仓库外的上层全局规范里 |
| 已有分发通道 1：`AGENTSMD_BLOCKS` 四个规则块 → `vibe apply-agentsmd --confirm` 合入项目 AGENTS.md | `scanner.py:227-232`、`missing_agentsmd_blocks` :235-262 |
| 通道 1 的 sentinel 覆盖率：4 块里**只有 2 块**有版本化 sentinel（`VIBE_ENTRY` :206、`ENGINEERING_PRINCIPLES` :224）；`CAPABILITY` 与 `PRD_GUIDE` 没有。且 sentinel 的**唯一读取点在 `initializer.py:370-391`**，`missing_agentsmd_blocks` 只读 MARKER、从不读 sentinel | `grep -rn "CURRENT_SENTINEL" vibe_guide/` |
| 已有分发通道 2：vibe **自有**协议物化到 `.vibe/proposals/skills/<name>/SKILL.md`，写一次永不改写——**只覆盖 prd-guide 与 vibe-entry 两个随包协议**，与外部 skill 安装是两条互不相连的路径 | `initializer.py:335-346` |
| 分发通道 3 **只完成了一半**：`install_skill()`（`skills.py:316-391`）功能完整——clone、校验 SHA、物化、算 `installed_tree_sha256`、原子替换——但①**全仓无生产调用点**（只有 `tests/test_skills.py` 的 8 处调用，另 1 处 import）；②`vibe install` 是 **vibe 自身的安装/升级状态机**（`cli.py:99` choices → `cli.py:867-885` → `run_install_or_upgrade`），与 skill 无关，且 parser 只有一个位置参数、多传一个子路径会被 argparse 拒；③记录写的是 `$VIBE_HOME/skills/<name>.json`，键为 `source/sha/tree/installed_tree_sha256/timestamp/validation`，**既没有 `name` 也没有 `commit`**，也**不写 `.vibe/config.json`** | `grep -rn "install_skill" vibe_guide/ tests/`；`skills.py:333-368` |
| `.vibe/config.json` 的 `skills[]`（形状确为 `{name, source, commit}`）是 **scanner 的只读输入**，由用户/外部写入，没有任何代码产出它 | `scanner.py:87-148` |
| 已有分发通道 4：visible-sdd 节点合同携带 `sdd_protocol` 指针 | `monitor.py:2944` |

### 1.2 pm-ai-shipping 实际内容（clone 后实读）

| skill | 行数/字节 | 内容 |
|---|---|---|
| `code-review` | 253 行 / 14.5KB + 3 个 references（correctness-taxonomy 144 行、security 68 行、performance 55 行） | 以 correctness 为核心引擎，perf/security 为同引擎子案例 |
| `intended-vs-implemented` | 42 行 / 3.8KB | 审「文档声称」与「代码实际」之差 |
| `shipping-artifacts` | 79 行 / 8.8KB | 定义 `documentation/` 意图文档集，是上一条的前置输入 |
| 合计 | **641 行 / 43KB ≈ 11k tokens**；reviewer 实际常用的核心（code-review SKILL + taxonomy）约 23KB ≈ 6k tokens | |

**可移植性实测**：`pm-ai-shipping` 里 `$ARGUMENTS`（Claude 专有插值）只出现在 `commands/` 的
**4 个**文件中（该目录共 5 个 md，`derive-tests.md` 计数为 0），**3 个 SKILL.md 一处都没有**。
即这三个是全仓 69 个 skill 里最干净、最容易跨宿主复用的一批，符合 vibeguide
"统一 CLI 连接多 Agent"的初衷。

### 1.3 已实测确认的阻断点

> 注意：下面调的是 `install_skill()` **函数本身**（临时 `VIBE_HOME`、`fetch=True`、真实网络），
> 不是某个 CLI 命令——如 §1.1 所述，今天没有 CLI 能走到这里。

```
install_skill(<pm-skills spec>, fetch=True)  →  status: pending, installed: False
  ├─ clone 成功、SHA 校验通过（vendor cloned: True）
  └─ 失败点唯一：skills.py:257  manifest = stage / 'SKILL.md'
     pm-skills 仓库根**没有** SKILL.md（它是 9 plugin × 69 skill 的 monorepo）
```

**即：今天这 3 个 skill 装不进 vibeguide。** 这不是配置问题，是安装器只支持"仓库根即 skill"一种形状。

失败时整棵树其实已正常物化（`.claude-plugin/`、`AGENTS.md`、`pm-ai-shipping/` 均在），只差根 manifest。
**该中间态无法靠只读命令复现**——`skills.py:389-391` 的 `finally` 会 `rmtree(stage)`；本次是直接调
`_materialize_commit(vendor, commit, stage)`（绕开外层 `finally`）后对 `stage` 做目录快照观察到的。

同时实测到解法可行：

```
git archive --format=tar <sha>:pm-ai-shipping/skills/code-review  →  归档根即 SKILL.md
```

子路径归档出来 **SKILL.md 正好落在归档根**，现有 manifest 检查和 `_safe_archive_members`
一行都不用改（git 2.50.1 实测）。

### 1.4 顺带发现的既有缺陷（本方案不修，只登记）

`VISIBLE_SDD_PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"`（`monitor.py:120`）是一个
**只在 vibeguide 自己仓库里解析得到的相对路径**。`initializer.py` 只物化 prd-guide 与 vibe-entry，
**从不物化 visible-sdd-worker.md**（`grep -n "visible-sdd-worker" vibe_guide/initializer.py` 零命中）。
所以在消费项目里，worker 被指向一个它读不到的文件，而交付门（`monitor.py:4080`）仍然只要求
`protocol` 字符串逐字相等——协议内容事实上没有送达，只有字符串对上了。**一个从未读过协议的 worker
与一个真正执行过协议的 worker，在这道门前完全不可区分。**

这直接影响本方案的落点选择（见 §3 阶段 2c）。建议单开 issue 处理物化缺失本身。

---

## 2. 这个 skill 到底效果如何（不吹不贬的评估）

### 2.1 好在哪：它和 vibeguide 踩过的坑是同一套哲学的更完整表述

`code-review` 的核心主张不是清单，是一个判据：**真缺陷几乎不在单个文件内，而在两个参与者之间的
"约定"上**（调用方↔被调方、生产者↔消费者、现在写入↔之后读取、两个本该产生同一状态的分支）。
按文件切分的审查看不见它们，因为两半从不同时在视野里。

逐条对上 vibeguide 自己写在 AGENTS.md 和失败记忆里的规则：

| code-review 的规则（原文要点） | vibeguide 已有的同源规则 |
|---|---|
| 跨边界缺陷必须**同时引用两侧**参与者，禁止只按文件分组 | AGENTS.md「跨模块约定必须有唯一真相来源」「测试必须覆盖跨模块契约，不只覆盖单模块逻辑」 |
| "Absorption is not prevention"——下游有缓存/重试/默认值恰好掩盖了缺陷，**不算反驳**；只有让执行**不可能**的机制才算 | 记忆《修之前先确认那段代码真的可达》《别借用为另一个文件写的判定》 |
| 测试通过、代码眼生、命名可疑、缺测试——**都只是待查线索，都不是证据，也都不是反驳** | 记忆《Reviewer 意见须实测复现》 |
| 第三种结局 `Unresolved`：关键契约或运行时事实未知时，**与 findings 分开列**，不算发现也不算通过 | AGENTS.md「未知状态不能当成无事项或成功」「记录 `blocked_unknown`」 |
| 明确告诉 review worker "你在读不在改"，并在收尾确认工作树未变 | `visible-sdd-worker.md` §2 git 写禁令清单、§4 reviewer 独立性违规 |
| "It works nearly always" 描述的是竞态，不是反驳 | AGENTS.md「95% 不够，必须追求极致可靠」 |

最有价值的一条是 **refutation 门**：一个候选要成为 finding，必须同时具备①有依据的约定 ②可行的执行
③具体矛盾 ④可观察后果 ⑤**已经检查过的最强反论**。

它还点名了两类"强 agent 几乎从不上报"的缺陷（来自一次真实代码库植入缺陷的评测）：
**权威值调和**（请求值 ≠ 生效值，下游还在读请求值）和 **身份/关联**（结果找不回发起实体）。
这两类正是 vibeguide 自己 run log 里反复出现的形态。

### 2.2 不好在哪 / 不匹配处（必须在方案里处理，不能装看不见）

1. **它是静态审查，达不到 vibeguide 的证据标准。** 原文自己写明「A static review produces
   code-review findings, not confirmed exploits or measured regressions」。而 vibeguide 的 P0 是
   资金/数据正确性、要求实测复现。→ 两者是**组合关系不是替代关系**：skill 负责"找得准、不瞎报"，
   vibeguide 的"验证命令须为绿"仍然是独立一道门。方案里必须写死这一点。
2. **它不定义严重度刻度**（Report 契约里只有 `[Severity]` 占位）。→ 这其实是好事：P0–P3 可以直接绑进去，
   不冲突；但**必须外部给出映射表**，否则 agent 会按英文语感自定级别。
3. **Parallelism 一节涉及 fan-out 与模型选择。** 上游其实**已经写了"一层封顶"**
   （SKILL.md:190「One level of fan-out is the target; if delegation is unavailable or the scope is
   small, run the same procedure sequentially.」），所以这条不是需要新增的适配，而是需要**显式重申**
   ——理由是 vibeguide 侧的真实顾虑：会话内嵌套子代理**不计入** `max_active_worker_sessions`
   预算（`config.py:31`），上游的"目标值"在这里必须是硬上限。"要求跑最强模型、禁止混模型"
   （SKILL.md:193-199）核对属实，与 `model_router.py` 需要对齐。
4. **`intended-vs-implemented` 要求存在 `documentation/*.md` 意图文档集**，否则按它自己的规则
   "文档缺失本身就是第一条发现"——vibeguide 项目没有这套文档，会导致**每个节点第一条发现都是同一句废话**。
   → 必须重新锚定到 vibeguide 的**节点合同**（输入/输出/错误行为/验收示例）。
   **但这四个字段不是机器派生的**：`node_spec.py:39` 的 `contract` 属产品 spec 形状（agent 按
   prd-guide §4 写的内容），不在 `ENGINEERING_CONTRACT_FIELDS`（:63-66）里；且 `cli.py:493-500`
   会把没有 `contract` 的旧计划回填成**四个空字符串**，全仓未见非空校验。
   → 所以重锚必须配一条兜底规则：**合同字段为空时按 `Unresolved` 处理，不得当成一条 finding**，
   否则原失败模式原样复现。
5. **全英文**，vibeguide 协议全中文。worker 读英文无障碍，但严重度映射必须用中文写死。
6. **69 个里只有这 3 个属于审阅层**，其余 66 个是产品方法论。不要因为这 3 个好就整包塞进 reviewer 上下文
   （43KB 已经不算小，整包会挤掉 diff 的位置）。

### 2.3 评估结论

这 3 个 skill 值得接，但**只值得接进 reviewer 这一个位置**，且需要 4 处适配（严重度映射、
intent 重锚到节点合同 + 空合同兜底、fan-out 硬上限、静态/实测分工写明）。它不是"顺手能用"，
是"方法论层严丝合缝，接口层要改 4 处，而通道本身还得先接通"。

---

## 3. 结合方案

### 设计约束（来自 vibeguide 自己的规则，不可违反）

- AGENTS.md §7：**不把 Skill 源码复制进本项目**，只记来源与 SHA → 不 fork、不 vendor。
- AGENTS.md §7：**项目只保存引用和版本，不保存绝对用户路径** → 任何指针不得写死 `/Users/...`。
- prd-guide §0：**agent 出内容，vibe 出协议 + 校验** → 方法论属"内容"，归 agent 侧 skill；vibe 只加引用与校验。
- AGENTS.md §1：统一 CLI 连接多 Agent → 不走 Claude marketplace 分发路径。
- K2/K3：最简实现、只改必须改的 → 不动事件字段，不加 contract 字段。

### 阶段 0（前置，必做）：把 skill 安装通道接通，并支持 monorepo 子路径

**为什么不能绕过**：唯一的替代是 fork pm-skills、把 3 个 skill 提到仓库根——那等于复制 Skill 源码
（违 §7），且要自己永久跟上游。

**r1 的估算错在哪**：r1 写「改动 ~80 行、S1 估 10」，是把这件事当成"给现有命令加一个参数"。
实测后（§1.1 通道 3）真实范围是三段：

| 段 | 内容 | 今天的状态 |
|---|---|---|
| A. CLI 面 | 新增 skill 安装子命令 + 参数解析（`name` 与 `subdir` 必须是两个参数——`skills.py:17` 的 `_NAME` 正则不允许 `/`） | **不存在**。`vibe install` 已被 vibe 自身的安装/升级占用，不可复用 |
| B. 子路径能力 | `SkillSpec.subdir`；`_materialize_commit` 的归档 spec 由 `commit` 改为 `f'{commit}:{subdir}'`；subdir 入口校验（拒绝绝对路径、`..`、首尾 `/`，只允许 `[A-Za-z0-9_.-]` 与 `/`） | 不存在，但**已实测归档语义可行**，manifest 检查与 `_safe_archive_members` 不用改 |
| C. 记录落地 | 把安装结果写进 `.vibe/config.json` 的 `skills[]`（`{name, source, commit}` + 新增 `subdir`），让 `vibe scan` 能看见。**写入必须是 append / 按 `name` 幂等合并，不得整体重写 `skills[]`**——该数组今天是纯用户输入，代码首次写入时必须保留已有条目 | **不存在**。今天 `install_skill` 只写 `$VIBE_HOME/skills/<name>.json`，两套记录互不相连 |

**附带需要定的一件事**：记录里的 `tree` 字段（`skills.py:350,360`）来自 `_verify_vendor`，是**仓库根 tree**；
subdir 模式下它不再对应实际物化内容（对应的是 `installed_tree_sha256`）。保持原样还是改记子树，需明确。

**测试先行（红→绿）**
1. 无根 SKILL.md 的仓库 + 合法 subdir → `installed`（今天是 `pending`，已实测）。
2. `subdir` 含 `..` / 绝对路径 / 符号链接目标 → `pending`，不落盘、不写记录。
3. 不带 `subdir` 的既有安装路径行为逐字不变（回归）。
4. 同仓多 subdir 装成多条记录，`vibe scan` 全部报 valid。
5. **CLI 往返断言**：`install` 子命令产出的记录直接喂给 `scanner._configured_skills` 判 valid
   （记忆《签发方与校验方的契约漂移》——这类断言无天然归属者，须显式指定）。

**规模（修正后）**：1 个 PR，A+B+C 三段，**S1 重估 14（轻规划上沿）**——步骤 4、范围 3（cli/skills/scanner/config 四处 + 测试）、
不确定 2（归档语义已实测）、失败代价 2（新增命令，不影响存量路径）、读码 3。

### 阶段 1：只装 3 个，不装 69 个

阶段 0 落地后，用新子命令按 `name` + `subdir` 两个参数各装一条（具体命令形状由阶段 0 的 CLI 设计确定，
r1 里 `vibe install pm-ai-shipping/skills/code-review` 这种把子路径当唯一参数的写法**不成立**）：

| name | subdir |
|---|---|
| `code-review` | `pm-ai-shipping/skills/code-review` |
| `intended-vs-implemented` | `pm-ai-shipping/skills/intended-vs-implemented` |
| `shipping-artifacts`（可延后，见阶段 3） | `pm-ai-shipping/skills/shipping-artifacts` |

三条记录同锁 commit `8607e3b…`。**不装** `commands/*.md`（Claude 专有、含 `$ARGUMENTS`）、
**不装** `.claude-plugin/plugin.json`（marketplace 打包元数据）、**不装**其余 66 个产品方法论 skill。

**验收**（r1 的验收不成立，已修正）：`vibe scan` 报 valid **不构成安装证据**——`scanner.py:135-139`
的 `valid` 只做三项纯语法判定（名字正则、40 位 SHA、source 可解析），手工往 `.vibe/config.json`
里写三条假记录也能通过。真实验收必须是：
- `$VIBE_HOME/skills/<name>/SKILL.md` 实际存在（`code-review` 另含 `references/` 三个文件）；
- `$VIBE_HOME/skills/<name>.json` 的 `installed_tree_sha256` 与实际物化树重算值一致；
- `.vibe/config.json` 出现 3 条记录且 `vibe scan` 全部 valid。

### 阶段 2（核心）：把方法论绑进 reviewer——三处文本，不改判定逻辑

**2a. `visible-sdd-worker.md` §1.2 扩写**（现在是一句话）

在"按 P0–P3 分级产出意见"之外补四条：
- 审查方法按 `code-review` skill 的 agreement 引擎推进：先确定跨边界约定 → 构造违反该约定的可行执行
  → 完成反驳 → 才算一条 finding。按文件逐个看不算审查。
- **节点合同**（输入/输出/错误行为/验收示例）就是本节点的 documented intent；按
  `intended-vs-implemented` 的方法审「合同声称」与「代码实际」之差，两侧都要引用到行。
  （不使用该 skill 原文的 `documentation/*.md` 锚点。）
- **合同四字段为空时**（旧计划回填产物，见 §2.2 第 4 条）按 `Unresolved` 处理并上报，
  **不得**把"合同为空"本身当成一条 finding。
- **fan-out 硬上限一层**（上游写的是目标值，这里是硬上限，理由见 §2.2 第 3 条）；会话内不得再派
  子子代理，规模小时直接串行。审查子代理沿用 §2 的 git 写禁令清单。

**2b. 新增严重度映射表**（写进同一协议，中文，这是把两套契约对齐的那一块）

| code-review 的 finding 字段 | vibeguide 落点 |
|---|---|
| Expectation + Evidence | 被违反的约定 = 节点合同条款 / 跨模块契约 |
| Trigger + Defect | 复现路径；**vibeguide 要求实测复现，不接受纯静态推断**（静态审查产出的是候选，不是结论） |
| Impact | 决定 P 级：资金/数据正确性 → P0；逻辑缺陷/事务边界 → P1；边界条件/行为回归 → P2；其余 → P3 |
| Refutation | **必填**。未完成反驳的候选不得计入 P0–P2（只能落 P3 或 Unresolved） |
| Unresolved | 落 `blocked_unknown`，既不计入 clearance 也不算通过 |
| Coverage | 写进 §3 证据链；"零发现"必须表述为"在已审范围内无成立发现"，不得表述为"无缺陷" |

**这一步收益的准确表述**（r1 此处过度承诺，已修正）：本方案**不改** `monitor.py:4066-4080`，
交付门改动前后**完全不变**，仍只校验三项：clearance 三键为非 bool 整数 0、`evidence_ref` 为非空字符串、
`protocol` 与 `VISIBLE_SDD_PROTOCOL_REF` 逐字相等——**这三项都不反映反驳是否完成**。
「未反驳不得计 P0–P2」是加给 agent 的**协议文本约束，vibe 侧没有任何机器判据可验证它**。
按 AGENTS.md §11「不把 marker / 文件存在当成业务批准」，这条收益只能记为"提高 reviewer 自律的
文本约束"，不能记为"门变严了"。已进 §6 未验证项。

**2c. 新增第 5 个 AGENTS.md 分发块 `## Review Methodology`**（`scanner.py` `AGENTSMD_BLOCKS`）

**为什么必须有这一块**：§1.4 已证明消费项目拿不到 `vibe_guide/protocols/visible-sdd-worker.md`。
唯一可靠送达消费项目的通道是 AGENTS.md 分发块。

**指针写什么**（r1 写的 `.vibe/proposals/skills/code-review/SKILL.md` **是悬空路径**——没有任何代码会
创建它：`.vibe/proposals/skills/` 目录下只有 vibe **自有随包协议**（prd-guide/vibe-entry）与一个
`proposal.md`（architecture-skill-pack 提案），由 `initializer.py:322`、`:335-346` 写入；
外部安装的 skill 落在 `$VIBE_HOME/skills/<name>`，两条路径互不相连）。两个可选落点：

| 方案 | 代价 | 评价 |
|---|---|---|
| **推荐**：指针写 `${VIBE_HOME:-$HOME/.vibe-guide}/skills/code-review/SKILL.md`（**带默认值的变量形式**，不写死 `/Users/...`） | 零新增代码 | 与 AGENTS.md §7「用户级共享缓存由 `VIBE_HOME` 指定；项目只保存引用和版本」一致。**但必须带默认值**，理由见下方 ⚠️ |
| 备选：阶段 0 增加"把已安装 skill 物化到消费项目 `.vibe/proposals/skills/`"的能力 | 阶段 0 再加一段新工作量 | 与现有 prd-guide/vibe-entry 形态统一、项目自包含；但把外部 skill 内容复制进每个项目，与 §7 精神有张力 |

**⚠️ 裸 `$VIBE_HOME` 会复现 §1.4 的同一种失效**：`VIBE_HOME` 在 vibe 的 Python 里**有默认值**
（`paths.py:73`：`os.environ.get("VIBE_HOME") or str(Path.home() / ".vibe-guide")`），但那个默认值
**只存在于 vibe 进程内，不会传给读 AGENTS.md 的 agent**——`grep -rn "vibe_home" vibe_guide/` 在
`paths.py`/`skills.py` 之外零命中，`vibe scan --json`（`cli.py:178-187`）与 `vibe doctor` 的 facts
（`doctor.py:65-80`）都不输出解析后的 `vibe_home`。环境变量未导出时 shell 把 `$VIBE_HOME/skills/...`
展开成 `/skills/code-review/SKILL.md`（文件系统根下的必然不存在路径），而**未导出正是 vibe 支持的默认形态**
（本机实测：`VIBE_HOME` 未设置，默认解析为 `~/.vibe-guide`，该目录不存在）。
所以指针必须写成带默认值的 `${VIBE_HOME:-$HOME/.vibe-guide}` 形式；若改为让 `vibe scan --json` 输出
`vibe_home` 字段，则属阶段 0 的新增代码，此落点就不再是"零新增代码"。

块内容 4–5 行，只写指针与三条硬规则：节点合同即 intent（为空按 Unresolved）；未反驳不计 P0–P2；
未知落 `blocked_unknown`。

同时提供 `REVIEW_METHODOLOGY_MARKER` 与版本化 `REVIEW_METHODOLOGY_CURRENT_SENTINEL`
（教训来源：PR #81「证明块存在的标记本身也要版本化」；以及 v4.9 finding「判定不得依赖英文头部套话」
——sentinel 取中文正文片段）。

**⚠️ sentinel 必须同时接上读取点**：`missing_agentsmd_blocks`（`scanner.py:235-262`）**只读 MARKER，
从不读 sentinel**；两个现存 sentinel 的唯一读取点是 `initializer.py:370-391` 的陈旧块提醒。
只在 `scanner.py` 加常量而不在 `initializer.py` 加对应判断，就正好复现记忆《没人读的指纹不算校验》。
故改动清单必须含 `initializer.py` 的第三段 sentinel 判断。

**测试**：`missing_agentsmd_blocks` 对新块的存在性判定；纯中文 AGENTS.md 不误判；
前 4 块已应用时新块仍可达（现有逐块判定已保证，补回归用例）；
**`initializer.py` 对新 sentinel 的陈旧块提醒有测试覆盖**（否则该 sentinel 无读取点）。

**规模**：1 个 PR，约 140 行，文本 + 测试为主。S1 估 11（轻规划）。

### 阶段 3（建议延后）：验收侧接 shipping-artifacts

把 `shipping-artifacts` / `derive-tests` 的意图文档集与测试覆盖图纳入 `.vibe/runs/` 的验收证据要求。

**建议延后的理由**：它要求项目具备一整套 `documentation/`，对存量项目是一次大改造；而阶段 2 已用
**节点合同**替代了 intent 来源，收益的大头已经拿到。等阶段 2 在 1–2 个真实 run 上跑过，再用实际数据
判断是否值得。

### 阶段 4（另一条线，不在本方案范围）

前段 66 个产品方法论 skill → `prd-guide` §2 五话题的引用补丁。
**建议与本方案分开做**：受众不同（阶段 2 面向 reviewer 子代理，阶段 4 面向与用户对话的 agent）、
失败代价不同（阶段 4 影响产品决策质量，阶段 2 影响代码正确性判定）。可以并行推进，但不要合成一个 PR。

---

## 4. 明确不做什么

- 不 fork、不 vendor pm-skills 任何文本进 `vibe_guide/`（§7）。
- **不动 `review.py`**——它是未接线代码（§1.1，全仓无生产调用点），按记忆《修之前先确认那段代码真的
  可达》不在本方案里改。顺带记录一个**不修的已知缺陷**：`review.py:39`
  `sorted((item.severity ...), reverse=True)[0]` 取的是字符串字典序最大值，对 `"P0".."P3"` 会选出
  **`P3`（最低级）**而非最高级；现有测试两条都是 `P1`，覆盖不到。该函数不可达，故只登记不修。
- 不新增事件字段，因此不触碰 `state._EVENT_DATA_KEYS`（记忆：允许名单外的事件键落盘时被静默丢弃）。
- 不给节点 contract 加字段，避免 contract digest 漂移触发 fail-closed（`monitor.py` 交付门）。
  阶段 2 全部是协议文本 + AGENTS.md 块，不进 digest。
- 不改 `monitor.py:4066-4080` 的交付门判定逻辑（见 §2b 的收益修正）。
- 不自动追踪上游 main：记录锁 SHA，升级走一次显式安装动作。
- 不把这 3 个 skill 装成 Claude marketplace plugin（会把能力绑死在单一宿主，违 §1）。

## 5. 风险

| 风险 | 处置 |
|---|---|
| 上游迭代导致方法论漂移 | SHA 锁定 + Freshness: Review-on-use；升级是显式动作 |
| reviewer 上下文变大（+6k tokens） | 只装 3 个不装 69 个；references 按需读（skill 自身设计就是"选中子案例才读对应 reference"） |
| 会话内嵌套 fan-out 绕过并发预算 | 阶段 2a 写死"一层硬上限" |
| agent 按英文语感自定严重度 | 阶段 2b 中文映射表为唯一判据 |
| 静态发现被当成实测结论 | 阶段 2b 写死"静态产出是候选"；vibeguide 的验证命令门不放宽 |
| 与 Claude Code 内置 `/code-review` 同名 | 引用一律用完整路径（见 §2c 的落点表），不用裸 slash 名 |
| 空节点合同导致重锚失效 | 阶段 2a 的空合同兜底规则（按 `Unresolved` 处理） |

## 6. 未验证项（按能力合同如实登记）

- 阶段 0 的代码**尚未编写**。`git archive <sha>:<subdir>` 已在本机 git 2.50.1 实测通过，
  但 CLI 面（A）、subdir 接进 `install_skill` 全链路（B）、`.vibe/config.json` 记录落地（C）
  三段**全部未验证**。
- 上游 commit `8607e3b` 之后的变化未跟踪（本次审阅为只读，未做网络动作）。
- 本方案**未在任何真实 DAG run 上验证过收益**；阶段 2 完成后需要至少 1 个真实节点跑通才能给出效果结论。
- `intended-vs-implemented` 重锚到节点合同的实际效果**为设计推断，未实测**。
- **阶段 2b 的「未反驳不得计 P0–P2」是纯文本约束，vibe 侧无机器判据**；改动前后交付门完全不变，
  仍只校验 clearance 三键为 0、`evidence_ref` 非空、`protocol` 逐字相等。它是否真能改变 reviewer 行为，未验证。
- **`${VIBE_HOME:-$HOME/.vibe-guide}` 指针未在真实消费项目里验证过**：本机 `VIBE_HOME` 未设置、
  默认目录 `~/.vibe-guide` 不存在（因为今天没有任何 CLI 走过安装通道）。阶段 0 落地后需实际验证
  该指针在消费项目的 agent 会话里可解析。
- **空节点合同的发生率未测**：`cli.py:493-500` 的空串回填只影响没有 `contract` 的旧计划，
  实际有多少存量计划落在这一支，未统计。
- 3 个 skill 的审查质量**未在 vibeguide 真实 diff 上盲测**，§2 的评估来自读文本。

## 7. r1 → r2 修订清单（独立 review 后，逐条实测复核）

| 级别 | 问题 | 修订 |
|---|---|---|
| P1 | §2c/§5 指向的 `.vibe/proposals/skills/code-review/SKILL.md` 不会被任何步骤创建 | §2c 给出两个落点及推荐（`$VIBE_HOME` 变量形式） |
| P1 | `vibe install <skill>` 这条 CLI 不存在；`install_skill()` 无生产调用点；记录不写 `.vibe/config.json` | §1.1 通道 3 重写；阶段 0 拆为 A/B/C 三段，S1 由 10 重估为 14；阶段 1 命令形状改写 |
| P2 | 「每块自带 MARKER + 版本化 SENTINEL」——实际只有 2/4 块有 | §1.1 新增一行如实记录 |
| P2 | sentinel 的唯一读取点在 `initializer.py`，不在 `scanner.py` | §2c 补 ⚠️ 段与对应测试项 |
| P2 | §2b「0 才第一次有了含义」过度承诺 | 改写为文本约束，并进 §6 |
| P2 | 「让 reviewer 自己 fan out」——上游本就写了一层封顶 | §2.2 第 3 条改为"显式重申为硬上限" |
| P2 | 节点合同四字段是 agent 写的产品内容，且可回填为空串 | §2.2 第 4 条补实测；§2a 增空合同兜底规则；§6 补一条 |
| P2 | 「AGENTS.md 同句」——本项目 AGENTS.md 无该句 | §1.1 改为"在仓库外的上层全局规范里" |
| P2 | 阶段 1 验收「`vibe scan` valid」不构成安装证据 | 阶段 1 验收改为三项实证 |
| P2 | 阶段 1 命令形状与阶段 0 的 name/subdir 双字段设计矛盾 | 阶段 1 改用表格，说明 name 正则不允许 `/` |
| P2 | 「`$ARGUMENTS` 出现在 5 个文件」——实际 4 个 | §1.2 更正 |
| P3 | `review.py` 是未接线代码，且 `sorted(reverse=True)[0]` 取的是 P3 不是最高级 | §1.1 与 §4 如实记录，按"不可达不修"处理 |
| P3 | §1.3 的"整棵树已物化"中间态无法只读复现 | §1.3 注明观察手段（直调 `_materialize_commit` 绕开 `finally`） |
| P3 | 阶段 0 未提 `tree` 字段在 subdir 模式下的语义 | 阶段 0 补"附带需要定的一件事" |
