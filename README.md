# Vibe Coding 辅助开发向导

[English README](README.en.md)

> 如果这个项目对你有帮助，欢迎点一下右上角的 ⭐ Star，让更多人发现它。

Vibe Guide 是一个本地 CLI：先扫描项目和规划任务，再用一次精确授权启动可恢复的开发、独立 Review 与返工流程。它把技术交付、Review 接受、push、MR、merge 和 deploy 分开记录；默认授权永远不包含 deploy。

## 安装

当前发行版本为 `4.9.1`。版本真相以仓库中匹配的 `v4.9.1` tag、对应提交和 Release 资产为准；版本引用的写法约定：反引号 v 形式（如本句的 tag）只用于当前 release tag，历史版本写作 V4.5.0 这样大写、无反引号的形式，否则 release-tag-gate 会判为旧 tag 残留。`dist/` 目录只是本地构建缓存，不是下载入口。交付验收分别覆盖 wheel、sdist 和源码安装；本地构建只生成验证用产物，不创建 tag/Release、push、merge 或 deploy。

### 安装与初始化前提

- **运行环境**：Python ≥ 3.9，零第三方依赖（纯标准库）；pip 或 pipx 安装，wheel / sdist / 源码三种方式均可（三种安装方式均有测试钉住协议文件随包发布）。
- **装后验证**：`python3 -c "import vibe_guide; print(vibe_guide.__version__)"` 与 `vibe --help`。
- **初始化顺序**：进入项目先 `vibe scan --json`（只读）；确认初始化才 `vibe init --confirm`；AGENTS.md 规则块经 `vibe apply-agentsmd --confirm` 人工评审后合入。
- **宿主框架零要求**：入口协议完全自包含。宿主装有强制门/状态行类框架（如对齐门卡片）时，S1 评分并入其首个强制状态行作为 `S1：` 字段输出，不另起一轮评分或第二个门；未装则独立输出一行 S1 结果。
- **存量项目升级**：物化文件（`.vibe/proposals/skills/*/SKILL.md` 与 AGENTS.md 已合入块）永不自动改写；新版 init 检测到与随包内容不一致时给出漂移提示，人工 diff 后手动合并。

### 给 Agent 的安装协议

当用户要求安装或升级时，Agent 必须按以下顺序执行并回报证据：

1. 先查询最新 tag，不根据 README、`dist/` 文件名或历史记忆判断版本：
   `git ls-remote --tags --sort='-v:refname' https://github.com/halfmoon82/vibe-guide-automation.git 'refs/tags/v*'`
2. 选择最新稳定 tag（当前应为 `v4.9.1`），核对 tag、提交和可下载 Release 资产版本一致；无法核对时保持 `unknown`，不要猜测。
3. 在目标环境安装该版本，并验证：
   `python3 -c "import vibe_guide; print(vibe_guide.__version__)"`
   `vibe --help`
4. 安装完成后进入用户指定项目，先运行只读的 `vibe scan --json`；只有用户明确确认初始化时，才运行 `vibe init --confirm --json`。
5. 初始化后回报 `.vibe/session-contract.json` 和初始化结果；安装成功不等于项目已初始化，也不等于已授权监工、push、merge 或 deploy。

下载包后 Agent 不应假设安装器会自动运行向导或写入项目；初始化必须作为独立、可见、可追溯步骤执行。

升级与回滚必须显式指定已验证的版本：从 V2.0.0 安装后执行 `pip install --upgrade` 到 V3.10.0，确认版本和 CLI，再使用 V2.0.0 构建产物 `--force-reinstall` 回滚并重新确认版本。`.vibe/` 迁移另按 V3.10 安装流程执行；provider、缺失 Python 版本和远端 Release 能力未验证时保持未验证。

Python 3.9 legacy 环境（已验证为 pip 21.2、setuptools 58，且未预装 wheel）使用 setuptools 自带的 editable `develop` 命令，不依赖环境偶然存在的 wheel：

```bash
python3 -m venv .venv
.venv/bin/python setup.py develop
.venv/bin/vibe --help
```

现代 Python/pip 使用隔离构建；`pyproject.toml` 明确提供 setuptools 和 wheel 构建依赖，不要求用户预装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/vibe --help
```

两条路径都不升级或修改系统 Python。legacy 命令仅用于声明的 Python 3.9 兼容路径；现代环境不再关闭 build isolation。

也可以直接运行：

```bash
python3 -m vibe_guide --help
```

## 核心命令

```text
vibe scan                                    只读扫描项目，不创建 .vibe/
vibe init --confirm                          确认后初始化最小项目状态；重复执行不改写
vibe doctor                                  报告可观察的环境、Skill 和 Agent 命令事实
vibe attest --adapter <id> --facts <json>    登记本会话实测到的能力（复杂计划发布前必做一次）
vibe plan --request <请求>                   先走 S0/S1 分流；complex 请求生成 draft
vibe plan --request <请求> --from-prd <spec> 用产品级 spec 发布复杂计划，工程字段自动派生
vibe plan --print-protocol                   打印 PRD 引导协议（agent 用）
vibe authorize --plan <ID> --authorize AUTHORIZE   记录一次用户授权（十节点门禁证据）
vibe monitor --plan <ID> --authorize AUTHORIZE     启动监工；没有精确授权时拒绝
vibe status --plan <ID>                      读取当前快照，不轮询外部 provider
vibe resume --plan <ID>                      从快照、任务登记和事件证据继续
```

新会话入口：agent 先按 `.vibe/proposals/skills/vibe-entry/SKILL.md` 的入口协议在会话内自评 S0/S1——<=8 直接执行、9-15 轻规划，均不触碰 vibe，评分后输出一行 S1 评分与档位；>15 或拿不准时才 `vibe scan` + `vibe plan --request --s1` 进入正式路由。该协议完全自包含，不依赖任何外部技能。

产品经理的完整路径（自评确认为复杂之后）：agent 按 `.vibe/proposals/skills/prd-guide/SKILL.md` 的协议引导写 PRD 与节点拆分（业务字段），`vibe attest` 登记会话能力，`vibe plan --from-prd` 发布，`vibe authorize` 授权，`vibe monitor` 派发。旧的 `--node-spec` 手写路径仍可用。

每个命令都支持 `--json`。默认文本面向产品经理；JSON 适合桌面 App 适配器和自动化调用。退出码为：`0` 命令成功执行，`2` 参数错误，`3` 需要确认或因设计变化阻塞，`4` 外部/运行状态未知。

## 扫描和初始化

`scan` 只返回可观察事实，不推测登录、审批、merge 或 deploy 权限，也不写任何文件。

```bash
vibe scan --json
vibe init                 # 退出 3，不写入
vibe init --confirm       # 创建缺失的最小 .vibe/ 结构
```

已有 `AGENTS.md` 不会被覆盖。缺失规则时只在 `.vibe/proposals/agentsmd/` 生成建议（能力真相、复杂请求入口、`New Session Entry`、`Engineering Principles` 四个规则块），由 `vibe apply-agentsmd --confirm` 经人工评审后合入。外部 Skill 的安装不由 `init` 隐式触发。

### V2 能力合同（监工与 worker 共用）

确认初始化后，`.vibe/session-contract.json` 是当前项目会话的唯一能力事实源；监工入口和 worker/provider action 都读取同一份合同。初始化只记录安全、有限且当场可验证的运行时事实，任务终端、浏览器控制和可见会话在未探测前保持 `unknown`。

合同中的能力状态包括 `verified_available`、`not_exposed`、`permission_denied`、`probe_failed`、`unknown_timeout`、`unknown` 和过期后的 `stale`。合同缺失、损坏或无法核验会安全映射为 `blocked_unknown`；过期能力只会降为 `stale` 并要求刷新，不会被解释成“没有工具/能力”或成功；`unknown_timeout` 也不会降级为 `unavailable`。

`AGENTS.md` 不会被初始化直接改写，能力真相规则只生成到 `.vibe/proposals/agentsmd/proposal.md`。规则要求监工和 worker 不得根据记忆、README、工具未被提及或自然语言自报判断能力存在与否，必须引用合同事实；没有证据时保持未知并重试或阻断。真实 provider 的权限、登录、任务可见性、创建/续接和外部故障仍须在真实平台核验，合同和本地测试不能替代该核验。

## 简单任务和复杂计划

明显简单的请求直接走轻量路径，不生成 PRD 或 DAG：

```bash
vibe plan --request "修正标题错别字" --json
```

疑似复杂的请求必须显式提供五维 S1 分数和已关闭产品决策的 node-spec。CLI 不替用户猜节点拆分：

```bash
vibe plan \
  --request "实现两个可并行模块并完成独立审查" \
  --s1 4,4,4,4,4 \
  --plan-id example-plan \
  --node-spec plan-source.json \
  --json
```

node-spec 是 JSON 对象，包含 `title`、`objective`、已批准的 `decisions`、Agent `capabilities` 和 `nodes`。每个节点沿用公开 `DAGNode` 合同，至少提供输入、输出、错误行为和验收示例。复杂计划会原子发布 PRD、Spec、Issue、DAG、机器可读 plan/nodes 和授权卡；已有同名计划不会被覆盖。

## V4.4 恢复与兼容性

V4.4 将工程故障限定在节点范围内，并保留同一任务身份。五类恢复状态是：`repairable`（修复 dirty checkout、detached HEAD 或 branch drift）、`retryable`（超时、断线、任务创建失败等可重试故障）、`capacity_wait`（429 或 provider 容量不足，按退避等待）、`binding_unknown`（无法证明原 task、writer、lease 或 cursor 身份，只阻塞该节点及其依赖闭包）和 `external_decision`（产品、权限、deploy、凭据或不可逆安全决策）。

修复必须回到原 developer task、generation、writer、worktree、lease、cursor 和合同摘要；不得创建 successor 或第二 writer。Review 的 P0–P2 缺陷回到原 developer 返工，再由同一 reviewer 复审。无关节点仍按硬依赖重新计算 ready set，`integration_after` 和 workflow 证据不会阻塞可执行节点。

迁移使用显式命令 `vibe migrate-state --preview`（只读预览）和 `vibe migrate-state --confirm`（生成迁移证据）。旧 V2–V4.3 snapshot、事件、授权 epoch 和 cursor 保存在历史 namespace，只读且不会恢复为当前可执行状态；迁移生成 `history_manifest.json` 与 `migration_evidence.json`，保留源文件哈希，无法完整解释时标记 `historical_incomplete`。安装、升级、迁移、回滚分别验证；回滚必须留下目标版本、来源构建产物和验证命令证据。

测试通过、worker 自报、timeout、分支名或本地 fixture 都不能单独证明真实 provider 能力、任务可见性、merge、发布或 deploy。当前执行必须以匹配的 plan、授权、BindingIntent/BindingProof、lease、provider 返回和验收证据为准；缺失或冲突证据保持 `unknown`，不得转成成功。

## 授权、监工和恢复

### DAG 运行时约束

每个 Agent Task 都是某个 Issue 的运行时实例；任务线程、Provider 返回值或 worker 自报不会替代 Issue/DAG 合同。节点完成独立验收后，Monitor 立即依据最新的 `depends_on` 状态重算 `ready_set`，已解锁的节点可以继续调度，不等待整批任务形成 barrier。

### V4.6 派发拓扑与并发上限

V4.6 起，DAG 真并行的载体是每节点一个可见 worker 会话（Codex 为 `create_thread` 创建的 user-owned thread）；监工只派发、等待、收口，不作任何节点的 writer。任务登记 `tasks.json` 用 `topology` 字段记录每个节点的派发拓扑：

- `visible-sdd`：每节点一个可见会话，会话内走 SDD 双角色——dev 子代理实现，review 子代理以独立上下文、只读审查（协议见 `vibe_guide/protocols/visible-sdd-worker.md`），返工与复审在同一会话身份内闭环；
- `dual-visible`：保守默认，developer 与 reviewer 是两个不同的可见独立任务；平台能力 UNKNOWN 时 fail-closed 到本拓扑，不会升级为 `visible-sdd`；
- `background`：平台无可见桥接时的显式降级，必须在能力报告、授权卡和交付三处披露降级及限制（不可见、不可直接进入、返工续接受限）；`mode=background` 缺少披露时授权卡机器校验直接失败。

平台拓扑由适配器注册表的 `DISPATCH_TOPOLOGY_MATRIX` 按各平台 `in_session_sdd` 探针证据裁定。`in_session_sdd` 与 `visible-sdd` 分属两层、不互换：前者是适配层的名字——既是 manifest 能力探针字段名，也是 `DISPATCH_TOPOLOGY_MATRIX` 的裁定值；后者是 `topology` 字段的枚举值（派发层），由监工把裁定值翻译而来，描述节点实际派发拓扑；探针通过不等于 topology 已是 `visible-sdd`（以矩阵裁定为准）。

并发上限：`.vibe/config.json` 的 `max_active_worker_sessions` 控制同时活跃的 worker 会话数，默认 5（合法范围 1–64），与授权卡快照取较小者生效；显式但非法的值是配置错误，不会静默回落默认值。节点验收、P0–P2 清零且证据登记后归档会话，名额释放给后续 ready 节点。

### V4.1 复杂任务最终整合

复杂任务在授权前必须完成 PRD 阶段的范围与边界确认，并把需求、PRD、Spec/Issue、DAG 审计、计划确认、授权卡和用户授权纳入十节点强制链；节点只能通过结构化记录完成，显式跳过会记录用户指令、原因和后继策略：允许后继时继续，跳过授权节点则不能推导执行授权。所有业务节点完成后，独立 reviewer 执行只读的最终整合 Review，核验 PRD/Spec 合同、全量 diff 与 P0–P2 门禁。CLI 会区分“局部节点完成”“整合 Review 进行中”“整合 Review 返工”和“整合通过但外部动作未授权”。

该整合流程只适用于 `complex`；`simple` 与 `light_plan` 仍走轻量路径，不生成最终整合 Review。整合通过不等于 merge、push 或 deploy：授权卡的 `remote_git_actions` 仅是远端 Git 总开关，deploy、凭据和系统权限始终需要独立授权与验证。

当前版本中，确认授权卡后需要两步：`vibe authorize --plan <ID> --authorize AUTHORIZE` 记录十节点门禁证据，再 `vibe monitor --plan <ID> --authorize AUTHORIZE` 启动 Monitor。旧版 CLI 兼容路径仍可接受显式 `--authorize`：

```bash
vibe monitor --plan example-plan --json
```

没有授权的显式兼容调用仍退出 `3`，且不会启动 runner。确认授权卡范围后，确认动作应直接调用等价的 Monitor 启动路径：

```bash
vibe monitor --plan example-plan --authorize AUTHORIZE --json
vibe status --plan example-plan --json
vibe resume --plan example-plan --json
```

节点从 `planned` 开始。授权确认启动 Monitor 后，当前无硬依赖节点立即调度；provider 的 `complete` 只映射为 developer 的 `delivered`；随后必须由不同 reviewer 任务给出 `accepted`，硬依赖才会解锁。全部节点 accepted 后，run 才是 `complete`。

恢复以 `.vibe/runs/<run-id>/state.json`、`tasks.json` 和 `events.jsonl` 为准。已登记的活动 handle 不会因重复 `resume` 创建第二 writer。计划或节点合同变化会持久化为 `blocked_design` 并使旧授权失效；provider unknown/timeout 会保持 `blocked_unknown`，不会转成成功或无事项。

已确认规则能够唯一判断、且仍处于当前项目、plan revision、授权文件/action 和非 deploy 边界内的实现纠偏，由监工自动执行并记录，不会再次作为产品取舍询问。纠偏证据必须绑定已批准决定、授权和 Issue 合同；未绑定文本不能冒充用户决定。合同变化后的 `monitor --plan <ID> --authorize AUTHORIZE` 会在同一 run 上审计旧授权与变更原因，保留原任务身份和 cursor，登记新授权后续接修正 DAG；旧任务终止或 continuation 无法证明时仍会 fail closed。

桌面 App 原生能力不能由 Python 直接调用时，public CLI 使用 `.vibe/provider-actions/` 的 provider-neutral request/result bridge。`monitor/resume` 会写入有界、digest 绑定的 `create/locate/visibility/resume/wait` 请求；App 会话按 `native_tool` 调用公开能力并回写绑定结果。Codex 映射到 `create_thread`、`navigate_to_codex_page`、`send_message_to_thread` 和 `wait_threads`；其他 provider 必须先接入并验证自己的等价桌面控制面。未完成原生探针时不允许 background fallback；在真实 task ID、host、定位和可见性全部核验前，状态保持 `blocked_unknown`；node-spec 中的 provider/mode/thread/host 自声明不会成为证据。

授权 action 是 closed allowlist；未列明、deploy-like、外部安装和系统权限动作一律拒绝。`files` 必须是规范化的项目内相对路径列表，并与授权卡的 file scope 精确绑定。授权卡还绑定同时活跃的 developer/reviewer pair 上限；只有 reviewer 接受、P0–P2 清零且证据登记后才归档 pair 并释放容量。

## LocalRunner 边界

`LocalRunner` 只能执行构造时由适配层登记的精确命令；adapter ID 或任一命令参数不一致都会在启动前拒绝。它不使用 shell 字符串，不持久化命令参数、stdout、stderr、token、credential 或 provider 原文，只保存有界的 PID、退出状态、命令名称、安全事件引用和确认命令来源。

本仓库的端到端测试使用临时项目与 fake Agent 命令，验证本地进程、授权、developer→reviewer、恢复、unknown 和 deploy 排除。这些证据不证明真实 Codex、Claude Code、Cursor、Grok、WorkBuddy、Kimi Code 或 DeepSeek Harness 的登录、权限、任务可见性、创建/续接行为，也不证明 push、MR、merge 或 deploy 已发生。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m vibe_guide --help
python3 -m compileall -q vibe_guide tests
git diff --check
```
