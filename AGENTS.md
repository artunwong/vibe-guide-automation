# Vibe Coding 辅助开发向导

## 1. 项目目标与边界

本项目提供一个通用的辅助开发向导：

- 用统一 CLI 连接 Codex、Claude Code、Cursor、Grok、WorkBuddy、Kimi Code 和 DeepSeek Harness 等 Agent；
- 帮项目生成或适配开发规则；
- 对简单任务保持轻量，对复杂任务先完成需求讨论、PRD、Spec/Issue 和 DAG；
- 经一次明确授权后，自动并行开发、测试、Review、返工和验收；
- 在所有受支持桌面 App 的完整可见自动化路径中，每个 developer 和 reviewer 都是用户可见、可进入、可继续的独立任务；
- 只在产品设计变化、需要外部授权或 deploy 时请求人类。

当前逻辑项目根是本目录。上层 CFO 仓库的 `AGENTS.md` 仍然适用；本文件只增加本项目规则，不替代或修改上层规则。

设计基线：`docs/superpowers/specs/2026-08-24-vibe-coding-development-guide-design.md`。
实施计划：`docs/superpowers/plans/2026-08-24-vibe-coding-guide-implementation-plan.md`。

## 2. 工作方式

- 先判断缺口是否会改变范围、成功标准、授权或外部影响；仅在会改变这些结果时提高信息问题，其他可逆工作记录假设后继续。
- 能简单就简单；不要为了未来可能使用而增加模块、依赖或门禁。
- 输出优先使用产品经理能理解的语言；只在必要时提及代码、接口、worktree 等术语。
- 只改当前任务直接涉及的文件；发现无关问题只提醒，不顺手修改。
- 技术完成、Review 通过、交付授权、merge 和 deploy 分开表达。

## 3. 目录与模块边界

- `vibe_guide/`：CLI、扫描、规划、DAG、授权、状态、监工和 Agent 适配器。
- `tests/`：单元、契约、状态恢复、权限降级和端到端夹具。
- `.vibe/`：项目运行配置、项目知识、计划、授权、状态快照和事件日志；不得保存凭据。
- `docs/superpowers/specs/`：已确认设计和需求合同。
- `docs/superpowers/plans/`：实施计划和 DAG。
- `README.md`：面向用户的安装、扫描、规划、授权、监工和恢复说明。

模块只通过明确的数据结构和接口交互。CLI 负责参数和输出，不能把扫描、规划或监工逻辑全部写进 `cli.py`。Agent 适配器只描述能力和调用方式，不复制核心流程。

## 4. 复杂任务流程

每条请求先经过 S0 规则预筛；疑似复杂时进入 S1 五维评分：步骤数量、知识域、不确定性、失败代价、工具链复杂度。

- `<=8`：直接执行；
- `9-15`：轻规划；
- `>15`：进入复杂流程。

复杂流程必须按以下顺序推进：

```text
需求讨论 → 产品决策卡 → PRD → Spec/Issue → DAG 审计
→ 开发计划确认 → 一次授权 → 监工执行 → 独立验收
```

产品取舍不能留到监工阶段。未闭合的产品决策不得进入 `authorized`。

## 5. DAG 与并行规则

默认优先并行，只把真正阻塞启动的关系写成硬依赖：

- `depends_on`：硬依赖；未完成时不能启动；
- `integration_after`：联调或收口关系，不阻塞独立开发；
- `contract`：占位实现必须满足的输入、输出、错误行为和验收示例；
- `parallel_group`：可以同时启动的节点组。

可用 stub、fixture 或 fake service 时，应先并行开发，再做适配联调。共享文件或潜在冲突不是天然串行理由，优先使用隔离 worktree。

## 6. 监工与授权

短触发词：`启动监工`、`推进开发`、`执行DAG`。

触发词只启动预检。涉及监工、并行开发或外部动作时展示授权卡，列明 DAG 范围、ready 节点、并行组、worker、worktree、文件范围、测试/Review/返工/验收权限，以及 Git 远端动作组是否包含。

用户明确授权后，一次授权覆盖当前 DAG 已列明的全部非 deploy 动作。deploy、未列明动作、扩大范围或系统权限变更必须单独授权。

“显式独立任务”是通用产品的首选合同，不是当前项目的临时执行规则。受支持 Agent 应优先通过对应桌面 App 的原生任务能力创建用户可见任务。Codex App 使用 `create_thread` 创建 user-owned thread；Claude Code、Cursor、Grok、WorkBuddy、Kimi Code 和 DeepSeek Harness 由各自适配器探测等价的创建、进入、续接和状态定位能力。平台确认没有等价桥接时，可以明确降级为 background subagent，但必须披露不可见、不可直接进入和返工续接受限，不得把降级模式标成完整可见自动化。每个 Issue 固定绑定一个开发任务；每次独立 Review 固定绑定另一个 reviewer 任务。可续接时返工回到原开发任务、复审回到原 reviewer 任务；降级模式无法保证时必须提前披露。

通用任务登记必须保存 `provider`、`mode=visible|background`、平台任务 ID（如有）、host、worktree、branch、状态/交付路径和续接 cursor/token；Codex 可见绑定具体保存 `threadId`、`hostId` 和 cursor。完整可见模式中，可见、可进入、可追溯是验收条件。无等价能力的平台可降级为 background subagent，但能力报告、授权卡和交付必须明确标识降级及限制。

任务数量上限默认约束“同时活跃”的 developer/reviewer 对，不是整个 DAG 生命周期内累计创建的任务数。一个 Issue 的 developer 与 reviewer 均完成、P0–P2 清零且证据登记完成后，应关闭或归档其独立任务；历史身份和 handoff 仍保留在任务登记中，但不再占活跃并发名额。归档不得发生在返工或复审仍可能回到原任务之前，也不得通过删除登记来伪造空闲容量。后续 ready Issue 可以在释放的名额内创建新的独立任务。

监工规则：

- 一个 DAG 节点只允许一个有效 writer；
- visible-sdd 拓扑下，reviewer 独立性由会话内独立子代理保证（同一 worker 会话内运行，上下文隔离、只读审查、不得代改业务代码）；dual-visible 拓扑下，developer 与 reviewer 仍必须是两个不同的可见独立任务；
- 优先启动所有没有硬依赖冲突的 ready 节点；
- 并发上限按当前未完成、未归档的活跃 worker 会话数计算（visible-sdd 每节点一个会话；dual-visible 每对 dev/reviewer 各一个）；已完成并归档的任务不占名额；上限值 = min(授权卡快照, 项目 .vibe/config.json max_active_worker_sessions)；
- Review 缺陷优先退回同一 worker；
- 返工后复审回到同一 reviewer，保留任务身份、cursor 和证据链；
- 保留旧证据，新增返工和验收证据；
- 计划或范围变化使旧授权失效；
- 未知状态不能当成无事项或成功；
- 无法验证时记录 `blocked_unknown`，不得伪造完成；
- 设计变化只重建受影响的 DAG 后缀，已锁定成果不重做；
- 监工中断后从 `.vibe/runs/` 的快照和事件日志恢复。

可见任务发生工具丢失时采用 `visible successor` 恢复：先确认原任务 aborted/archived 并保留 thread/cursor 与零写入证据；successor 写入前必须复核原任务已终止、唯一 writer、指定 writer worktree/branch、冻结 HEAD 和 clean state。App 自动创建的 host worktree 只作为工具宿主，不能替代或修改合同指定 writer root；任一证据不匹配即 fail closed，不创建第二 writer 或额外 worktree。

一致性纠偏按“用户当前明确决定 → 已批准 PRD/Design Spec → 授权卡 → Issue 合同 → 下层实现”取证。若这些证据只产生一个答案，且修正仍在已授权项目、DAG 和非 deploy 边界内，监工应记录纠偏、更新受影响的 DAG 后缀或合同并自动继续；不得因可唯一解决的实现不一致、命名不一致或过期下层合同中断用户。只有仍存在多个会实质改变产品结果的答案、产品范围或方向变化、需要外部/deploy/系统权限授权，或证据无法区分安全结果时才暂停。

**已确认规则能够唯一判断的事项由监工自动执行并记录，不得作为盲区或产品取舍再次询问；只有无法唯一判断且会改变产品方向、授权边界或外部承诺的事项才中断。** 自动纠偏证据必须绑定当前项目摘要、plan/revision、已批准决策摘要、授权摘要和 Issue 合同摘要；未绑定文本不能冒充当前用户决定。授权因合同变化失效后，公开重新授权应在同一 plan/run 上保留旧授权、变更原因、新授权摘要、原任务身份和 cursor，继续修正后的 DAG。

## 7. 配置与外部 Skill

- `architecture-skill-pack` 必装；`akasha-grimoire` 和 `aligning-with-johari-windows` 可选。
- Skill 只能从已配置 GitHub 地址拉取，记录来源和提交 SHA；不把 Skill 源码复制进本项目。vibe 自有的 PRD 引导协议（`vibe_guide/protocols/prd-guide.md`）随包发布、由 `init` 物化为 `.vibe/proposals/skills/prd-guide/SKILL.md`，不属于外部 Skill，不受此条约束。
- 用户级共享缓存由 `VIBE_HOME` 指定；项目只保存引用和版本，不保存 token、密码或绝对用户路径。
- `scan` 只读；`init`、Skill 安装和 `.vibe/knowledge/` 初始化必须经确认。
- 已有 `AGENTS.md` 只生成补丁建议，不直接覆盖、追加或删除。

## 8. 测试与验证

开发采用测试先行：行为或安全边界变更先写能复现问题的失败测试，再做最小实现；低风险文档和机械变更使用匹配的检查。正式交付仍运行定向和完整测试。

默认验证命令：

```bash
python3 -m unittest discover -s tests -v
python3 -m vibe_guide --help
git diff --check
```

必须覆盖：S0/S1 边界、产品决策门、占位契约并行、DAG 环检测、授权摘要和 deploy 排除、唯一 writer、同 worker 返工、快照恢复、未知状态、桌面 App 权限降级、扫描不写入和初始化幂等。

不能只用 mock 宣称完成真实能力；真实 Agent、GitHub Skill 拉取或桌面 App 权限未验证时，报告为“未验证”并说明缺口。

## 9. 日志与证据

- `.vibe/runs/<run-id>/events.jsonl` 追加记录状态变化、worker、节点、时间、结果和阻塞原因；
- `state.json` 保存可恢复快照；写入必须原子替换；
- `tasks.json` 保存每个开发/Review 可见任务的 `threadId`、`hostId`、worktree、branch、状态/交付路径和 cursor；
- 每个节点保留交付、Review、返工和最终验收证据；
- 超时、worker 不可用、状态查询失败和权限不足必须记录具体原因；
- 不依赖某个 Agent 的全局线程索引作为唯一状态源；
- 不在日志、计划或聊天中写入凭据、token、密码、私有邮箱正文或无关个人信息。

## 10. Git、worktree 与交付

- 开发节点优先使用独立 worktree 或等价隔离目录；一个节点一个有效 writer。
- **本项目 worktree 路径约定**：所有 worker worktree 必须建在仓库内 `.worktrees/<slug>`（代码级来源：`vibe_guide/models.py` 的 `WORKTREE_PREFIX` / `node_worktree()`，自动派发的节点分支由 `node_branch()` 派生为 `node/<slug>`），不得使用桌面兄弟目录（`../<slug>`）。Claude Code 按会话 cwd 归属项目，仓库外目录拿不到本项目的权限/信任配置，`gh pr create` 等命令会被拦截（V4.7 实录见 run log 2026-09-23 §降级与偏差汇总第 2 条）。已验证的做法：worker 会话 cwd 保持主目录，所有命令以 `cd .worktrees/<slug> &&` 开头。监工手工建树示例：`git worktree add .worktrees/<slug> -b fix/<slug> origin/main`。
- 创建任一桌面 App 独立任务前先确认 provider、目标项目、起始分支和 worktree；Codex App 使用 `create_thread`。同一 Issue 不得同时存在内部 subagent 与可见任务两个 writer。
- 提交前只暂存当前任务白名单；禁止 `git add .` 和 `git add -A`。
- `commit`、`push`、创建 MR、`merge` 作为一个 Git 远端动作授权组；deploy 仍是独立动作，不得把 Git 组描述成发布。
- 未经明确授权，不 push、merge、deploy 或修改上层 CFO 仓库文件。
- 交付必须说明：改动范围、验证命令和结果、未验证项、未纳入路径、当前 Git 状态及是否执行 push。

版本交付固定规则：

- 只有具备可追溯远端 Release 或 tag、对应提交和可下载构建产物的版本，才算正式完整版本；分支名、提交标题、README、自报、本地 fixture 或测试通过不能单独证明正式发布。
- wheel、sdist 和源码安装必须分别验证；升级必须验证明确的旧版本到新版本路径。
- 包元数据版本、tag、Release 和构建产物版本必须一致。
- 安装、升级、迁移、回滚和 deploy 是独立动作，分别记录授权和验收证据；`commit`、`push`、创建 MR、`merge` 按 Git 远端动作组记录授权和验收证据。
- 远端代码平台不预设为 GitHub；只有当前环境探针已验证的 provider 才能执行远端动作。未知、超时或缺失证据保持 `unknown`/`blocked_unknown`，不得解释为成功或永久不可用。
- DAG 授权卡可预先列入受限 merge；用户确认该授权卡后，监工仅在独立终审通过、P0-P2 清零、目标与证据匹配且本地或远端绑定有效时自动 merge。该授权不包含 push、创建 PR、deploy、凭据或系统权限。

## 11. 安全与禁止事项

- 不绕过 Agent 沙箱、系统权限、Git 认证或外部平台审批。
- 不猜测 Agent 私有 API、平台能力、登录状态或 merge 权限。
- 不把网络/线程超时解释为没有任务。
- 不在未确认 writer、worktree 和文件白名单时重复派发。
- 不自动覆盖项目规则、外部配置、真实业务数据或用户原始证据。
- 不把测试通过、`PASS`、`CANMERGE`、marker 或文件存在当成业务批准、付款、发布或 merge 事实。

## 12. 短提示协议

当本文件、Spec、计划、测试和运行状态已经存在时，用户可以使用短提示：

- “扫描适配”；
- “初始化项目”；
- “继续规划”；
- “启动监工”；
- “继续监工”。

短提示不能绕过需求决策、DAG 确认、一次性授权、deploy 授权或安全边界。

任何产品设计、执行拓扑或任务可见性要求变化都会使当前 DAG 授权失效。此时先暂停旧任务、封存提交/未提交证据、更新 Spec/计划/授权卡，经用户重新确认后才可创建新任务继续。

# Vibe Guide capability contract proposal

## Capability and Tool Truth

- 不得根据记忆、README、工具未被提及或一次失败判断能力不存在。
- “当前会话未暴露”不等于“平台不具备该能力”。
- 超时、空响应和格式异常统一保持 UNKNOWN；`unknown_timeout` 不得转成 UNAVAILABLE。
- 监工和 worker 的自然语言自报不是能力证据。
- 能力判断必须引用 session contract 的 evidence_ref。
- 没有证据时请求 refresh 或报告 UNKNOWN，不得直接终止。
- 只有 runtime/provider 的结构化结果才能进入能力阻断状态。

## Complex Request Entry

- 复杂请求先按 `.vibe/proposals/skills/prd-guide/SKILL.md` 的协议引导：agent 出 PRD 内容与节点拆分，vibe 校验并派生全部工程字段。
- 产品 spec 只写业务字段；工程字段由 `vibe plan --from-prd` 派生，写了会被拒。
- `needs_confirmation` 项未闭合不得发布；产品决策只有产品经理选定后才是 approved。

## New Session Entry

- 开发/改动/排查类请求（「排查」含只读日志/生产数据分析）先按 `.vibe/proposals/skills/vibe-entry/SKILL.md` 的入口协议在会话内自评 S0/S1，不逢任务必过 vibe。
- 凡经 S1 评分的请求，评分后输出一行评分与档位（形如 `S1：11→轻规划（不触碰 vibe）`）；>15 或拿不准时才 `vibe scan` 并 `vibe plan --request --s1` 进入正式路由；<=15 直接执行或轻规划。
- 宿主另有强制入口/状态行协议时，S1 评分并入其首个强制状态行作为 `S1：` 字段输出，不另起一轮评分或第二个门。
- 会话门阻塞必须停下报告，不得伪造或跳过。
