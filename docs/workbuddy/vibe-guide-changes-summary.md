# vibe-guide → WorkBuddy 适配：改动总清单

版本：vibe-guide **4.9.1** ｜ WorkBuddy / CodeBuddy Code CLI **2.137.1** ｜ Windows 11
Python 环境：`C:\Users\sunnywong\.workbuddy\binaries\python\envs\default`
包目录：`...\envs\default\Lib\site-packages\vibe_guide\`
验证项目：`E:\PycharmProjects\future\vibe-complex-test`（新建，未污染 trading_core4 / books_obsidian）

---

## 零、一句话结论

**规划 / 发布 / 授权 + 派发请求的生成，这一整段已经能跑通并全绿；真正把活干完的最后一环还没接上**——
不是 WorkBuddy 没有能力（官方已有 Daemon + Jobs HTTP API，本机实测跑通），
而是「谁来调这些工具」还缺一个执行体：vibe 只把工具名写进信箱，**执行方是 WorkBuddy 桌面会话**，
而那边目前还没有叫这些名字的工具（需要一个 MCP server，见第八节）。

三个层次分清楚：**① 写请求 — 已通；② 请求里工具名正确 — 已验证；③ 工具真的被执行 — 未接。**

---

## 一、代码改动（8 个文件，5 组可独立回滚）

### A 组｜规则文件识别：AGENTS.md → 也认 CODEBUDDY.md

WorkBuddy 读的是 `CODEBUDDY.md`，根本不看 `AGENTS.md`。原来只认 `AGENTS.md`，
导致 WorkBuddy 项目被误报 "missing AGENTS.md"，且 vibe 写出来的规则落在没人读的文件里。

| 文件 | 改动 | 行 |
|---|---|---|
| `scanner.py` | 新增 `RULES_FILE_CANDIDATES = ('AGENTS.md','CODEBUDDY.md','CLAUDE.md')` | 33 |
| `scanner.py` | 新增 `resolve_rules_file(root)`（拒绝 symlink） | 36 |
| `scanner.py` | `ScanReport` 增加字段 `rules_file` | 91 |
| `scanner.py` | `scan_project` 用真实文件名算 `agentsmd` / `agentsmd_exists` | 436-439 |
| `doctor.py` | 缺失提示用真实文件名；`facts['rules']` 增加 `file` | 34, 61, 80 |
| `cli.py` | `_scan_payload` 输出 `rules_file` | 182 |
| `node_spec.py` | 引用 `resolve_rules_file` 计算 `agents_ref` | 27, 210 |
| `initializer.py` | 新增 `_rules_target()`，写回时落到真实规则文件 | 14, 520-528, 564 |
| `paths.py` | 项目根标记补 `CODEBUDDY.md` / `CLAUDE.md` | 54 |

**兼容关键：`AGENTS.md` 排第一 → 现有 Codex / Claude 项目行为逐字节不变。**

### B 组｜宿主 skill 自动发现

原来 skill 记录必须是「github source + 40 位 commit」，WorkBuddy 本地 skill 无法登记，
一直报 "required Skill not configured"。

| 文件 | 改动 | 行 |
|---|---|---|
| `scanner.py` | 常量 `WORKBUDDY_ORIGIN` / `WORKBUDDY_LOCAL_SOURCE` / `_SKILL_MANIFEST` / `_SKILL_MANIFEST_SCAN_LINES` | 116-124 |
| `scanner.py` | `_is_real_dir()` | 127 |
| `scanner.py` | `workbuddy_config_dir()`（env `WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR`，否则 `~/.workbuddy`） | 131 |
| `scanner.py` | `workbuddy_skill_roots()`（项目级优先，用户级其次） | 148 |
| `scanner.py` | `_skill_manifest_name()`（有界 64 行解析 frontmatter，不引入 YAML 依赖） | 175 |
| `scanner.py` | `_host_skill_record()` / `_host_skills()` | 205 / 231 |
| `scanner.py` | `_merge_skills()`（配置优先，发现只填空，受 `_SKILL_LIMIT` 约束） | 249 |
| `scanner.py` | `scan_project` 合并宿主 skill | 446 |
| `doctor.py` | `facts['skills']` 增加 `host_provided` | 40-47, 84 |

**无宿主目录时返回空 → macOS / Codex 行为不变。**

### C 组｜config schema 扩展 `origin` + `path`

| 文件 | 改动 | 行 |
|---|---|---|
| `scanner.py` | `_is_within()` / `_resolved_roots()` | 272 / 280 |
| `scanner.py` | `_configured_host_skill()`（按磁盘证据校验：真目录、含 SKILL.md、不越出宿主根/项目根） | 296 |
| `scanner.py` | `_configured_skills()`：带 `origin` 走宿主分支，未知 origin **fail-closed** | 342, 392, 403 |

**老记录没有 `origin` 键 → 仍走原 github 分支，行为不变。**
明确否决过「合成假 40-hex commit + github source」的方案：会伪造 provenance，
且 `install_skill()` 拿去 git clone 必然失败。

### D 组｜`adapters/manifests/workbuddy.yaml` 补 `native_control_plane: true`

这是**复杂计划发布的唯一阻塞点**。

- `cli.py:308-313` 要求 `mode=='visible'` 且 `visible_automation`
- `base.py:252-255`：`visible = native_control_plane and shell and subprocess and worktree and create and enter and resume and wait`
- `native_control_plane` 默认只有 `id=='codex'` 为真
- 而 workbuddy.yaml 早就声明了 `provider: "workbuddy-visible"`、4 个 `visible_task.*` 探针、
  `background_fallback` —— 缺这个标记，这些声明全是死代码。**是作者加 workbuddy 时的遗漏**

### E 组｜`NATIVE_TOOL_MAP` 补 `workbuddy-visible`（派发映射表）

> 2026-09-30 追加。用户拍板：先做路线 A 的映射表部分，MCP server 留到下一步。

| 文件 | 改动 |
|---|---|
| `providers.py` | 新增 `WORKBUDDY_VISIBLE_PROVIDER = "workbuddy-visible"`（与 workbuddy.yaml 的 `provider` 对齐） |
| `runners/provider_action.py` | import 该常量；`NATIVE_TOOL_MAP` 增加一条，5 个动作：create / locate / visibility / resume / wait |

映射后的工具名：`workbuddy_job__create` / `__get` / `__list` / `__reply` / `__wait`。

**已验证**：

- `compileall` rc=0；三家 provider 都在表里，另外两家的条目**逐字节未变**
- 通过真实 runner 调 `_native_tool()`，5 个动作全部返回上述工具名
- 未知 provider 仍抛 `ProviderUnavailable`（fail-closed 未被破坏）
- **信箱级实测**：`monitor` 跑通后，`.vibe/provider-actions/requests/` 下生成 2 条请求，
  均为 `provider=workbuddy-visible` / `operation=create` / `native_tool=workbuddy_job__create`
  （对应 `engine-replay`、`ledger-sharding` 两个节点）

**⚠ 但还不能真正派发**：vibe 只负责把工具名写进信箱，执行方是 WorkBuddy 桌面会话。
这些工具名目前**没有任何实现**——需要挂一个 MCP server 把它们暴露出来（见第五节）。
在那之前，请求会一直 `pending`，节点停在「自动修复中」。

### 未改动（刻意不动）

- `_wincompat.py`：Windows 兼容层，已作为 **PR #84 合入上游**，v4.9.1 里自带
- `provider_action.py` 的 `NATIVE_TOOL_MAP`：2026-09-30 已按用户拍板补上（E 组），
  但**只补到"能写出正确的工具名"为止**；工具本身的实现留给 MCP server，不在这里伪造

---

## 二、环境改动

| 项 | 位置 | 说明 |
|---|---|---|
| 命令 shim | `C:\Users\sunnywong\.workbuddy\bin\workbuddy.cmd` | 必须 `.cmd` 后缀；win32 `shutil.which` 按 PATHEXT 拼后缀，裸名 `workbuddy` 永远匹配不到 |
| 用户 PATH | `HKCU\Environment\Path` | **本次才真正写入**（之前记录失真，见第五节） |
| skill 收编 | `C:\Users\sunnywong\.workbuddy\skills\` | 9 个 `architecture-*`，从 `D:\books_obsidian\.workbuddy\skills\` 复制，**D 盘原件保留** |
| 测试项目 | `E:\PycharmProjects\future\vibe-complex-test` | `CODEBUDDY.md` + `core/{ledger,engine,report}.py` + git 仓库 + `.vibe/` |

收编的 9 个 skill（均含 `SKILL.md`）：
`architecture-agents-md-contract` / `git-worktree-contract` / `module-contract` /
`observability-contract` / `release-contract` / `requirement-contract` /
`skill-pack` / `tech-stack-contract` / `test-ci-contract`

---

## 三、验证结果

### 3.1 单元级 V1–V11 全 PASS

| # | 检查 | 结果 |
|---|---|---|
| V1 | 9 个 architecture-* 就位，各含 SKILL.md | PASS |
| V2 | `shutil.which('workbuddy')` 命中 `.cmd` | PASS |
| V3 | `resolve_rules_file` 优先级（AGENTS.md 优先） | PASS |
| V4 | `scan --json`：`rules_file=CODEBUDDY.md`、`agentsmd_exists=true` | PASS |
| V5 | skills 含 `architecture-skill-pack`，`origin=workbuddy`、`valid=true` | PASS |
| V6 | doctor：`required_configured: true`、`host_provided` 齐全 | PASS |
| V7 | doctor 退出码 0（不是 3） | PASS |
| V8 | github 老记录 valid 且无 `origin`；非法/越权/错 origin 均 fail-closed | PASS |
| V9 | 无宿主目录（模拟 macOS/Codex）→ skills 为空 | PASS |
| V10 | `compileall` rc=0 + 50 模块 import 全通 | PASS |
| V11 | 复杂请求 `plan` → `route=complex`、score 18 | PASS |

### 3.2 复杂链实测（规划 → 发布 → 授权）

| 步骤 | 结果 |
|---|---|
| `plan --from-prd` 发布 | `status: ok`；`vibeguide_monitor` / `dag`；4 个 worker 全部 `dual-visible` |
| `authorize` | `authorization_granted: true`，**十节点全齐**（s0→…→user_authorization） |
| 节点派生 | 每节点各有 worktree（`.worktrees/<节点>-<hash>`）与 branch（`node/<节点>-<hash>`） |

### 3.3 收尾复跑 doctor（PATH 修好后）

```
status            : ok
diagnostic_status : ready
ok                : True
issues            : []
facts.agents      : {'available': ['workbuddy']}
facts.rules       : {'file': 'CODEBUDDY.md', 'present': True}
facts.skills      : required_configured=True, records_valid=True
provider_bridge   : null     <- 预期，见 4.2
```

---

### 3.4 信箱级实测（E 组后）

`monitor` 跑通后，`.vibe/provider-actions/requests/` 下生成 2 条请求：

```
action-bf1bbe395780ac644714790f2d044255.json
  provider    : workbuddy-visible
  operation   : create
  native_tool : workbuddy_job__create      <- 映射表生效
  issue_id    : engine-replay

action-ef9a60657dcbe4b9e54c0bec6f2bc525.json
  provider    : workbuddy-visible
  operation   : create
  native_tool : workbuddy_job__create
  issue_id    : ledger-sharding
```

同时节点状态从 `blocked_unknown` 变为 `running` / `provider create action is pending`。

> 这次是**受控试跑**：临时把 `visible_task.*` 置 true 才过得去 visible 门禁，
> 跑完**立即还原**为诚实值（`capabilities.json` 已恢复原样）。

---

## 四、还没通的部分，以及原因

### 4.1 自动派发（E 组之前的状态，现已推进）

**E 组之前**：`vibe monitor` 后节点落 `blocked_unknown`，报错
`no verified native control plane for provider workbuddy-visible`。
根因是 `runners/provider_action.py` 的 `NATIVE_TOOL_MAP` 只写了两个 provider ——
Codex（`codex_app__create_thread` / `navigate_to_codex_page` / `wait_threads` /
`send_message_to_thread`）和 Claude Code（`ccd_session__spawn_task` / `ccd_window__open_session_in` /
`ccd_session_mgmt__*`）。

**E 组之后（当前）**：`workbuddy-visible` 已进表，`monitor` 不再报这个错，
节点从 `blocked_unknown` 变成 **`running` / `provider create action is pending`**，
信箱里也真的写出了请求。**但工具还没人执行**，所以是"待处理"而不是"已完成"。

作者注释写明这是刻意设计："占位符名字会永远躺在信箱里，只有验证过原生控制面的 provider 才能派发。"

**两道独立的门**（对照实验已确认）：

| | 位置 | 判据 | 状态 |
|---|---|---|---|
| 发布门禁 | `cli.py:308-313` → `base.py:252-255` | `native_control_plane` | **已解**（D 组） |
| 派发门禁 | `provider_action.py:48` `NATIVE_TOOL_MAP` | provider 是否在表内 | **已解**（E 组） |
| 执行门禁 | 桌面会话里有没有这个工具 | MCP server 是否装并信任 | **未解**（下一步） |

失败模式是干净的：明确报错 + 隔离节点，不会伪装成功、不会傻等。
E 组之后变成"请求已入信箱、等待被取走"，同样不会伪装成功。

### 4.2 `provider_bridge: null` 是诚实的，不是 bug

`.vibe/provider-actions/capabilities.json` 里 `visible_task.*` 四项登记为 **false**
（试跑结束后主动从"全 true"回滚成诚实值）。于是 `cli.py:308-313` 的 visible 门禁不通过，
bridge 为 null。**它只影响派发，不阻塞规划/发布/授权。**

### 4.3 派发能力的调研结论：WorkBuddy 有，vibe 没接

官方叫 **Daemon 模式 + HTTP API**，派发部分是 **Jobs API**（`/api/v1/jobs`）。
**版本门槛：CodeBuddy Code CLI ≥ v2.130.0；本机 2.137.1 已满足，不用升级。**

| vibe 要的动作 | WorkBuddy 官方端点 |
|---|---|
| 开会话拿 id | `POST /api/v1/jobs` → 返回 `id` / `sessionId` |
| 定位进入 | `GET /api/v1/jobs/:id`（含 `webUrl`、`cwd`） |
| 等它跑完 | `GET /api/v1/jobs` 轮询 `state` + `settled`，或 `GET /api/v1/jobs/events` SSE |
| 发后续指令 | `POST /api/v1/jobs/:id/reply` `{"text": ...}` |
| 读事件流 | `GET /api/v1/jobs/:id/stream`（SSE）/ `/transcript` |
| 恢复 / 重启 | `POST /api/v1/jobs/resume`、`POST /api/v1/jobs/:id/respawn` |

**本机实测闭环全 200**：起 daemon（PID 31276，`http://127.0.0.1:8099`）→
`health`/`info`/`jobs`/`workers`/`dispatch-context` 全通 → `POST /api/v1/jobs` 派发返回
`id=26c26e32`、`state=done`、`settled=true` → 查详情 / 读 transcript / 删除全通 → `daemon stop`。

**Windows 硬约束**：官方文档明确「只有通过 `daemon start` 启动的 daemon 独立于发起进程存活，
`--bg` 会随 CLI 退出被回收」。真要接，必须先拉 daemon 再打 HTTP。
另外所有 `/api/v1/*` 必带 `X-CodeBuddy-Request: 1`（否则 403）。

---

## 五、本次收尾时发现并修掉的一个记录错误

之前报告写「shim 已加入用户级 PATH」，**实际没有落盘**。本次复跑 doctor 得到
`attention` + `no candidate Agent command found`，追查发现：

- `_AGENT_COMMANDS`（scanner.py:14-22）**本来就包含 `workbuddy`** —— 不是漏了候选命令
- 读注册表 `HKCU\Environment\Path` → **不含** `C:\Users\sunnywong\.workbuddy\bin`
- 但 `shutil.which('workbuddy', path=<shim 目录>)` → 命中 `...\workbuddy.CMD` —— shim 本身没问题
- 之前 V2 PASS 是因为在测试进程里临时改过 PATH，**没有持久化**

已用 `[Environment]::SetEnvironmentVariable('Path', ..., 'User')` 真正写入，
注册表复验 `CONTAINS=True` 且只出现 1 次。修复后 doctor 全绿（见 3.3）。

---

## 六、可提上游的 PR

A 规则文件解析 / B 宿主 skill 发现 / C 宿主 skill config schema / D workbuddy manifest 补 `native_control_plane`

统一约束：只用标准库；不碰 `_wincompat.py`；新逻辑按「目录/记录是否存在」分支，
不按 `sys.platform` 硬判，保证 POSIX / Codex 行为逐字节不变。

---

## 七、回滚方式

```bash
# 回滚 A/B/C 三组代码改动
pip install --force-reinstall vibe-guide==4.9.1
```

- D 组单独回滚：删掉 `adapters/manifests/workbuddy.yaml` 里的 `native_control_plane` 键
- shim：删除 `C:\Users\sunnywong\.workbuddy\bin\workbuddy.cmd`，并从用户 PATH 移除该目录
- 收编的 skill：删除 `C:\Users\sunnywong\.workbuddy\skills\architecture-*`（D 盘原件未动）
- 测试项目：整个删掉 `E:\PycharmProjects\future\vibe-complex-test`

---

## 八、下一步（已拍板，E 组已完成，剩 MCP server）

用户已定的两件事：

1. **路线 A，分两步**：映射表先做（**已完成，见 E 组**），MCP server 下一步
2. **daemon 策略：自动拉起，跑完自动停**

### 剩下的活：MCP server（约 200~300 行）

在 WorkBuddy 会话里暴露 5 个工具，内部打 daemon 的 Jobs HTTP API：

| 工具名 | 打到哪 |
|---|---|
| `workbuddy_job__create` | `POST /api/v1/jobs` |
| `workbuddy_job__list` | `GET /api/v1/jobs`（轮询 state / settled） |
| `workbuddy_job__get` | `GET /api/v1/jobs/:id` |
| `workbuddy_job__reply` | `POST /api/v1/jobs/:id/reply` |
| `workbuddy_job__wait` | `GET /api/v1/jobs/events`（SSE）或 `/:id/stream` |

外加 daemon 生命周期（start / status / stop，**跑完自动停**）。

落地方式：写本地 MCP server → 配进 `~/.workbuddy/mcp.json`
（「专家·技能·连接器 → 连接器 → 自定义连接器」）→ **需要手动点「信任」才生效**。

### 提醒

在 MCP server 装上之前，`.vibe/provider-actions/requests/` 里那 2 条请求会一直 `pending`，
`vibe status` 会显示节点「自动修复中」。要清掉的话直接删那个目录下的 json 即可。

---

## 九、提交记录

| 项 | 值 |
|---|---|
| 提交位置 | fork `artunwong/vibe-guide-automation`（原仓库 `halfmoon82/vibe-guide-automation` 只有 pull 权限） |
| 分支 | `windows-workbuddy-adaptation`（2 个 commit：feat 代码 + docs 文档） |
| 上游 PR | **#109** —— https://github.com/halfmoon82/vibe-guide-automation/pull/109 （`mergeable: clean`，+1011 / -14，11 文件） |
| 基点 | 上游 `main` @ `252c17cb`（v4.9.1 之后的 14 个 commit，均为 macOS / PRD-guide 方向） |
| 本次提交内容 | A/B/C/D/E 五组代码改动 + 两份说明文档（`docs/workbuddy/`） |
| 未包含 | `_wincompat.py`（Windows 兼容层）已作为 **PR #84 合入上游**，v4.9.1 自带，不在本分支 |

**与上游 main 的关系**：v4.9.1 tag → main 之间改动的 40 个文件里，只有 `vibe_guide/cli.py`
与本次改动有交集（差 84 行，均在文件后半段）。本分支的 cli.py 改动是在 **main 版本**上
按上下文定位重新插入的一行（`"rules_file": ...`，位于 `agentsmd_exists` 之后），
因此 PR diff 只含本次改动，不会带上任何回滚。

**回归自检**：提交前复跑本机 11 项回归，11/11 PASS（见第三节）。
