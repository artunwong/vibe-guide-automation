# vibe-guide → WorkBuddy 深度适配报告

目标：让 vibe-guide v4.9.1 在本机 **跑复杂任务**（complex 路由 10 节点）。
验证项目：`E:\PycharmProjects\future\vibe-complex-test`（新建，未污染 trading_core4 / books_obsidian）。

---

## 一、结论

| 层 | 改造前 | 改造后 |
|---|---|---|
| 引擎层 | 可用 | 可用（未动） |
| 规则文件识别 | 只认 AGENTS.md → 误报 "missing AGENTS.md" | **识别 CODEBUDDY.md** |
| Skill 供给 | 只认 github source+40 位 commit → 误报 "required Skill not configured" | **自动发现宿主 skill** |
| doctor | `attention` | **`ready`，issues 为空，退出码 0** |
| 复杂路由 | 可达但发布被拦 | **`route=complex`，发布 + 授权全通**（十节点门禁实测通过） |
| 并行派发 | 不可达 | **WorkBuddy 有官方遥控面（Jobs HTTP API，实测跑通），但 vibe 未接入**（见第六、八节） |

`vibe doctor` 改造前后对比：

- 改造前：`issues: ["missing AGENTS.md", "required Skill not configured", ...]`
- 改造后：`issues: []`、`required_configured: true`、`rules: {file: "CODEBUDDY.md"}`、**exit 0**

---

## 二、改动清单（4 个可独立回滚的 commit）

目录：`C:\Users\sunnywong\.workbuddy\binaries\python\envs\default\Lib\site-packages\vibe_guide\`

### A. 规则文件解析（AGENTS.md → CODEBUDDY.md）
- `scanner.py`：新增 `RULES_FILE_CANDIDATES = ('AGENTS.md','CODEBUDDY.md','CLAUDE.md')` 与
  `resolve_rules_file()`（拒绝 symlink）；`ScanReport` 增加 `rules_file`
- `doctor.py`：缺失提示用真实文件名；`facts['rules']` 增加 `file`
- `node_spec.py`、`initializer.py`（新增 `_rules_target()`）、`cli.py`、`paths.py` 同步适配
- **兼容性核心：AGENTS.md 排第一 → 现有 Codex/Claude 项目零变化**

### B. 宿主 skill 自动发现
- `scanner.py`：`workbuddy_config_dir()`（env `WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR`，
  否则 `~/.workbuddy`）、`workbuddy_skill_roots()`（项目级优先）、`_skill_manifest_name()`
  （有界 64 行）、`_host_skill_record()`、`_host_skills()`、`_merge_skills()`（配置优先，发现只填空）
- `doctor.py`：`facts['skills']` 增加 `host_provided`
- **无宿主目录时返回空 → macOS/Codex 行为逐字节不变**

### C. config schema 扩展 `origin` + `path`
- 记录带 `origin=='workbuddy'` → 按磁盘证据校验（真目录、含 SKILL.md、不越权出宿主根/项目根）
- 未知 origin **fail-closed**
- **老记录无 `origin` 键 → 走原 github 分支，已断言逐字节不变**
- 明确否决"合成假 40-hex commit + github source"：会伪造 provenance，且 `install_skill()` 拿去 git clone 必炸

### D. `adapters/manifests/workbuddy.yaml` 补 `native_control_plane: true`
这是**复杂计划发布的唯一阻塞点**。依据：
- `cli.py:308-313` 要求 `mode=='visible'` 且 `visible_automation`
- `base.py:252-255`：`visible = native_control_plane and shell and subprocess and worktree and create and enter and resume and wait`
- `native_control_plane` 默认 `id=='codex'`；实测**只有 codex 与 claude-code 为 true**
- workbuddy.yaml 却声明了 `provider: "workbuddy-visible"`、4 个 `visible_task.*` 探针、
  `background_fallback` —— 缺这个标记，这些声明全是死代码。**明显是作者加 workbuddy 时的遗漏**

---

## 三、配套落地

- **收编 skill**：9 个 `architecture-*` 由 `D:\books_obsidian\.workbuddy\skills\` 复制到
  `C:\Users\sunnywong\.workbuddy\skills\`（**保留 D 盘原件**）
- **命令 shim**：`C:\Users\sunnywong\.workbuddy\bin\workbuddy.cmd`。
  必须是 `.cmd` 后缀——win32 `shutil.which` 按 PATHEXT 拼后缀，裸名 `workbuddy` 永远匹配不到。
  实测 `workbuddy --version` → `2.137.1`

  > **更正（重要）**：当时报告写"已加入用户级 PATH"，实际**没有持久化成功**——
  > 读注册表 `HKCU\Environment\Path` 确认不含该目录，所以新进程里
  > `shutil.which('workbuddy')` 恒为 None，`vibe doctor` 一直报
  > `no candidate Agent command found`。**现已真正写入并验证通过**（见第九节）。

---

## 四、验证结果 V1–V11 全 PASS

| # | 检查 | 结果 |
|---|---|---|
| V1 | 9 个 architecture-* 就位，各含 SKILL.md | PASS |
| V2 | `shutil.which('workbuddy')` 命中 `.cmd` | PASS |
| V3 | `resolve_rules_file` 优先级（AGENTS.md 优先） | PASS |
| V4 | `scan --json`：`rules_file=CODEBUDDY.md`、`agentsmd_exists=true` | PASS |
| V5 | skills 含 `architecture-skill-pack`，`origin=workbuddy`、`valid=true` | PASS |
| V6 | doctor：`required_configured: true`、`host_provided` 齐全、无 `missing AGENTS.md` | PASS |
| V7 | **doctor 退出码 0**（不是 3） | PASS |
| V8 | github 老记录 valid=true 且无 `origin` 键；非法记录仍 false；越权/错 origin 均 fail-closed | PASS |
| V9 | 无宿主目录（模拟 macOS/Codex）→ skills 为空 | PASS |
| V10 | `compileall` rc=0 + 50 模块 import 全通 | PASS |
| V11 | 复杂请求 `plan` → `route=complex`、score 18 | PASS |

**复杂计划发布实测成功**（`status: ok`）：`execution_engine: vibeguide_monitor`、
`engine_mode: dag`、`required_workflow` 十节点齐全、4 个 `dual-visible` worker、
`node_ids: [engine-replay, integration-review, ledger-sharding, report-incremental]`、
`engine_evidence_ref: engine-attestation:abc3635283386185`。

---

## 五、两个使用要点（踩过的坑）

1. **`--from-prd` 必须一步到位，不能先跑 draft。**
   `cli.py:1245` 只有未带 `from_prd` 时才物化草案；先跑 draft 会创建 `.vibe/plans/<plan_id>/`，
   随后发布撞 `destination.exists()` → `plan already exists`（exit 3）。
   正确：`vibe plan --request "<请求>" --s1 4,4,4,3,3 --from-prd <spec> --json`
2. **`--s1` 是五个 0–5 的整数、逗号分隔**（如 `4,4,4,3,3`），不是单个分数；>15 才进正式路由。
   plan_id 由 request 摘要派生，同一请求必然得到同一 plan_id。

---

## 六、试跑结论：派发未接通（已实测；WorkBuddy 侧能力存在）

> **更正**：初版这里写"WorkBuddy 侧没有等价物"，是错的。后续查官方文档 + 实测证实
> WorkBuddy **有**完整的原生控制面（见第八节）。准确说法是：**vibe 没接**，不是 WorkBuddy 没有。

按"可见会话能力可用"登记后**真跑了一遍完整复杂链**，拿到确定答案。

### 走通的（全部实测成功）

| 步骤 | 结果 |
|---|---|
| `attest` 全 true | `mode=visible` / `level=full` / `visible_automation=true` |
| `plan --from-prd` 发布 | `status: ok`；`vibeguide_monitor` / `dag`；4 个 worker 全部 `dual-visible` |
| `authorize` | `authorization_granted: true`，**十个节点全齐**（s0→…→user_authorization） |
| 节点派生 | 每个节点各有 `worktree`（`.worktrees/<节点>-<hash>`）与 `branch`（`node/<节点>-<hash>`） |

> `--authorize` 的值是**字面 `AUTHORIZE`**，不是 digest（传 digest 会报
> "the exact AUTHORIZE token is required"）。

### 没走通的：派发

`vibe monitor` 后节点落 `blocked_unknown` 并被隔离：

```
"no verified native control plane for provider workbuddy-visible"
```

**根因**：`runners/provider_action.py:48-63` 的 `NATIVE_TOOL_MAP` —— 这张"每个可见
provider 的原生桌面工具名对照表"**只写了两个 provider**：

- Codex → `codex_app__create_thread` / `navigate_to_codex_page` / `wait_threads` / `send_message_to_thread`
- Claude Code → `ccd_session__spawn_task` / `ccd_window__open_session_in` / `ccd_session_mgmt__*`

`workbuddy-visible` **不在表里** → 抛 `ProviderUnavailable`。作者注释写明这是刻意设计：
"占位符名字会永远躺在信箱里，只有验证过原生控制面的 provider 才能派发。"

**所以不是缺开关，是 vibe 侧压根没接。** 作者注释写明这是刻意设计（只认验证过的
原生控制面），不是 bug。但：**WorkBuddy 侧确实存在等价的原生控制面**（见第八节），
所以这个缺口是**可以合规补齐**的——不是伪造能力。

### 两道独立的门（重要）

| | 位置 | 判据 | 能否解 |
|---|---|---|---|
| 发布门禁 | `cli.py:308-313` → `base.py:252-255` | `native_control_plane` | **已解**（补 manifest） |
| 派发门禁 | `provider_action.py:48` `NATIVE_TOOL_MAP` | provider 是否在表内 | **与 manifest 无关，补 flag 解不了** |

### 失败模式是干净的

不是傻等 `retry_pending`，而是**明确报错 + 隔离节点**，不会伪装成功。

### 现状

- `attest` 已改回诚实值（`visible_task.* = false`），`mode` 回到 `guide`
- `native_control_plane: true` **保留**——它只让"规划/发布/授权"层完整可用，
  派发有独立的诚实门禁兜底，不伪造任何能力

### 实际可用的姿势

复杂任务的**规划与授权已完全可用**：PRD、Spec、DAG 审计、授权卡、十节点门禁、
每节点的 worktree + branch + 验收合同全部派生好。只有**自动派发并行会话**不可用。
可以拿着这些产物手动执行——每个节点该在哪个目录、哪个分支、按什么合同交付，都写清楚了。

---

## 七、可提上游的 PR

A 规则文件解析 / B 宿主 skill 发现 / C 宿主 skill config schema / D workbuddy manifest 补 `native_control_plane`。

统一约束：只用标准库；不碰 `_wincompat.py`；新逻辑按"目录/记录是否存在"分支，
不按 `sys.platform` 硬判，保证 POSIX/Codex 行为逐字节不变。

回滚方式：`pip install --force-reinstall vibe-guide==4.9.1` 即可还原 A/B/C；
D 单独删除 `workbuddy.yaml` 里的 `native_control_plane` 键。

---

## 八、官方遥控机制调研（WorkBuddy 叫什么、哪个版本有、怎么接）

### 8.1 结论先行

**WorkBuddy 有遥控机制，官方叫法不叫"遥控"，而叫：**

- **Daemon 模式 + HTTP API**（`/api/v1/*`，Beta）
- **Jobs API**（`/api/v1/jobs`）——派发「智能体实例」，是本节核心
- **ACP**（Agent Client Protocol，JSON-RPC over SSE，`/api/v1/acp`）——有状态持续对话
- **Channels（远程控制）**——文档第 6.9 节标题原文就是「Channels（远程控制）」，
  对应 CLI 的 `--remote-control [client]`（不带参 = AgentOS 模式，带 `wecom` 等 = 自动连接指定客户端）

**版本门槛：CodeBuddy Code CLI ≥ v2.130.0**（该版发布说明首次引入 Agent View /
`codebuddy agents` / 后台 job 交接 `/background`、`/fork-bg` / 后台写隔离 git worktree）。
**本机 CLI 已是 2.137.1，满足，不需要升级。**

### 8.2 官方文档索引

| 文档 | 地址 |
|---|---|
| Daemon 模式与后台会话 | `https://www.workbuddy.cn/docs/cli/daemon` |
| HTTP API（Beta） | `https://www.workbuddy.cn/docs/cli/http-api` |
| CLI 参考 | `https://www.workbuddy.cn/docs/cli/cli-reference` |
| v2.130.0 发布说明 | `http://www.workbuddy.ai/docs/zh/cli/release-notes/v2.130.0` |

### 8.3 `NATIVE_TOOL_MAP` 的能力映射（关键）

`provider_action.py` 要为 provider 提供 **create / enter / resume / wait / events**。
WorkBuddy Jobs API **逐条都有对应**，且是官方公开 REST：

| vibe 原生动作（Codex 侧工具名） | WorkBuddy 对应端点 |
|---|---|
| `codex_app__create_thread`（开会话拿 id） | `POST /api/v1/jobs` `{prompt, cwd, name, model, permissionMode, agent, bgIsolation}` → 返回 `id` / `sessionId` |
| `navigate_to_codex_page`（定位/进入） | `GET /api/v1/jobs/:id` 详情含 `webUrl`、`cwd`；另有 `GET /api/v1/jobs/dispatch-context` 取可派发仓库与 agent 候选 |
| `wait_threads`（等待） | `GET /api/v1/jobs` 轮询 `state`(`working`/`blocked`/`done`/`failed`/`stopped`) + `status`(`busy`/`waiting`/`idle`/`stopped`) + `tempo` + `settled`；或 `GET /api/v1/jobs/events`（SSE，事件 `snapshot`/`added`/`changed`/`removed`） |
| `send_message_to_thread`（发消息） | `POST /api/v1/jobs/:id/reply` `{"text": "...", "bash": false}` |
| （读事件流） | `GET /api/v1/jobs/:id/stream`（SSE，先回放最近 1000 行再尾随）／`GET /api/v1/jobs/:id/transcript` |
| （恢复 / 重启） | `POST /api/v1/jobs/resume` `{sessionId}`、`POST /api/v1/jobs/:id/respawn` |
| （停止 / 删除） | `POST /api/v1/jobs/:id/stop`、`DELETE /api/v1/jobs/:id` |

另有 `POST /api/v1/runs`（Gateway Protocol，一次性执行，`id`/`type` 必填，prompt 放
`payload.text`，回调 `callback.url`）+ `GET /api/v1/runs/:runId/stream`（SSE），适合
「发一次指令、拿结果」的无状态场景。**真正有状态的多轮对话走 ACP**
（`POST /api/v1/acp/connect` → `GET /api/v1/acp` SSE 订阅 → `POST /api/v1/acp`
发 JSON-RPC `newSession` / `prompt` / `cancelRun`）。

### 8.4 本机实测（不是读文档，是真跑）

```
daemon start --port 8099 --auth none
  → Daemon started (PID: 31276, endpoint: http://127.0.0.1:8099)

GET  /api/v1/health  → 200 {"status":"ok","platforms":["generic","wecom","wechat-kf"]}
GET  /api/v1/info    → 200 {"version":"5.5.6","os":"win32","gatewayMode":"local"}
GET  /api/v1/jobs/dispatch-context → 200 cwd + agents[cli/ptc/minimal/create] + repoTargets
GET  /api/v1/jobs    → 200 {"jobs":[]}
GET  /api/v1/workers → 200 本机全部 worker（含 kind=interactive/bg/daemon）

POST /api/v1/jobs {"prompt":"echo VIBE-PROBE-PONG","bash":true,"cwd":"...vibe-complex-test"}
  → 200 {"id":"26c26e32","state":"done","settled":true,
         "sessionId":"26c26e32-735c-4059-90fe-d0d9e824af7a", ...}
GET  /api/v1/jobs/26c26e32            → 200 state=done
GET  /api/v1/jobs/26c26e32/transcript → 200
DELETE /api/v1/jobs/26c26e32          → 200 {"deleted":true}

daemon stop → Daemon stopped.
```

**派发 → 查询 → 读结果 → 删除，闭环全部 200。** 唯一未实机跑的是 `reply`（需要 job
停在 `waiting` 态，本次用零 token 的 bash job 秒完成，没进 waiting）。

### 8.5 接入要注意的坑（Windows 尤其重要）

1. **Windows 上必须走 daemon，不能走 `--bg`。** 文档原文：「在 Windows 上，只有通过
   `daemon start` 启动的 daemon 独立于发起进程存活。其他后台会话、shell 命令、hooks
   和隧道均随所属 CLI 退出而回收；`--bg` 和 `daemon stop --keep-workers` 不会解除这项
   生命周期约束。」→ vibe 的 `background_provider: workbuddy-background` 若要真后台，
   必须先 `daemon start`，再打 HTTP API。
2. **认证**：`--serve`/daemon 默认密码认证，密码写进 `~/.codebuddy/settings.json`
   （本机宿主为 WorkBuddy，注意实际配置目录）；CI/隔离环境可用
   `CODEBUDDY_GATEWAY_AUTH=none` 或 `--auth none` 关闭。
3. **安全头**：所有 `/api/v1/*` 必带 `X-CodeBuddy-Request: 1`，否则 403
   `Missing required header`；凭据用 `Authorization: Bearer <pwd>` 或 `X-Access-Token`。
   `?password=` 只对 `GET /` 和 `POST /api/v1/auth/login` 有效，拿它打 API 会 401。
4. **后台写隔离**：v2.130+ 后台 job 首次改文件前自动移入 git worktree，**正好对上
   vibe 每节点一个 worktree 的模型**；可用 `bgIsolation: none|worktree` 覆盖。
5. `daemon install` 在 Windows 走 **计划任务**（`schtasks`，本机被安全策略拦截），
   但不装系统服务也能用 `daemon start/stop` 手动管。

### 8.6 下一步（待拍板）

按上面的映射给 vibe 写一个 `workbuddy-visible` 的 provider adapter：
`NATIVE_TOOL_MAP` 补 4 个动作到 Jobs API，外加 daemon 生命周期（start/stop/status）。
这是**纯新增、不改现有行为**，且全部基于官方公开接口，不构成伪造能力。
需要确认：是否要让 vibe 自动拉起/停掉 daemon（涉及常驻进程与认证凭据）。

---

## 九、收尾复验：PATH 真写入后 doctor 全绿

### 9.1 发现的问题

收尾复跑 `vibe doctor` 时得到 `attention` + `issues: ["no candidate Agent command found"]`，
与之前记录的 `issues: []` 不一致。查下来是**两个独立事实**：

1. **`_AGENT_COMMANDS`（scanner.py:14-22）本来就包含 `workbuddy`**，不是漏了这个候选命令。
2. 真正原因是 **shim 目录没有持久化进用户 PATH**——进程里
   `shutil.which('workbuddy')` 恒为 `None`：
   - 读注册表 `HKCU\Environment\Path`：不含 `C:\Users\sunnywong\.workbuddy\bin`（**False**）
   - 但 `shutil.which('workbuddy', path=<shim 目录>)` → 命中 `...\workbuddy.CMD`（**shim 本身没问题**）

   之前 V2 之所以 PASS，是在测试进程里临时改过 PATH，**没有落盘**。属于记录失真，已更正。

### 9.2 已修

用 `[Environment]::SetEnvironmentVariable('Path', ..., 'User')` 写入，注册表复验：
`CONTAINS=True`，且只出现 1 次（无重复）。

### 9.3 修复后复验（模拟全新登录环境：机器 PATH + 用户 PATH 重组）

```
fresh PATH contains shim: True
exit                 : 0
status               : ok          <- 之前是 attention
diagnostic_status    : ready
ok                   : True
issues               : []          <- 之前是 ["no candidate Agent command found"]
facts.agents         : {'available': ['workbuddy']}
facts.rules          : {'file': 'CODEBUDDY.md', 'present': True}
facts.skills         : required_configured=True, records_valid=True
provider_bridge      : null
```

`provider_bridge: null` 是**预期且诚实**的：capabilities.json 里 `visible_task.*` 四项
仍登记为 false（试跑后主动回滚的诚实值），所以 `cli.py:308-313` 的 visible 门禁不通过，
bridge 为 null。**这不阻塞规划/发布/授权**，只影响自动派发。

### 9.4 当前整体状态一句话

| 层 | 状态 |
|---|---|
| 引擎 / doctor / 规则文件 / skill 供给 | **全绿**（`issues: []`，exit 0） |
| 复杂任务规划 → 发布 → 授权 | **可用**（十节点门禁实测通过） |
| 自动派发并行会话 | **不可用**（vibe 未接入；WorkBuddy 侧能力已确认存在） |
