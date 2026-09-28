# V4.10 开放 issue 集中修复：交付门可重报、协议送达、方法论接入、事件驱动监工

状态：approved
审核：reviewed

目标：一次性关闭 GitHub 上 6 个开放 issue（#80 #88 #91 #92 #93 #94）：worker 格式错误可同会话重报；visible-sdd 协议在任何平台都送达 worker；vibe 能按需安装 monorepo 子目录 skill 并送进项目；reviewer 与 PRD 引导获得外部方法论；监工改为事件唤醒、上下文可控，停止空转烧 token；清理不可达的旧升级代码。

## problem

- worker 漏写 delivery_evidence 时节点直接砖化为 blocked_unknown，而同类的漏写 in_session_review 已可同会话重报（#88）（来源：system_inferred）
- visible-sdd worker 的派发指令只有一句话，协议路径只在本仓库可解析，消费项目的 worker 读不到协议，交付门却只比对路径字符串（#93）（来源：system_inferred）
- vibe install 只装 vibe 自身；能装 skill 的函数无任何命令调用，且只支持仓库根即 skill，monorepo 子目录装不了（#92）（来源：system_inferred）
- reviewer 只有 P0–P3 分级定义，没有如何发现缺陷的方法，clearance 的 0 没有含义（#92）（来源：user_confirmed）
- PRD 引导协议不引用任何产品方法论（#94）（来源：user_confirmed）
- 监工靠 10 分钟心跳轮询推进，完工到验收最多闲置一个周期；监工在同一会话里无限累积上下文，amulet-mvp-v3 实测一次空转心跳约 4 次模型调用、每次读入 17–20 万 token（#91）（来源：user_confirmed）
- cli.py 的 upgrade_project 分支与 upgrade.py 物理不可达（#80）（来源：system_inferred）

## user_scenarios

- worker 交付格式写错，收到可恢复拒绝后在同一会话修正重报，节点不被隔离（来源：system_inferred）
- 在任意消费项目、任意平台派发 visible-sdd worker，worker 指令里就带着协议全文（来源：user_confirmed）
- 产品经理需要某个方法论时，agent 按需执行一条安装命令，skill 按锁定版本落到项目 .vibe/proposals/skills/<名>/ 并被协议引用（来源：user_confirmed）
- PRD 讨论到成功标准时，agent 可参考 north-star-metric 等方法论，用中文推进，不另存 markdown，来源标记与停等门不变（来源：user_confirmed）
- 每次心跳先跑本地预检（没事/有活/该换班）；没事约 2 轮结束；worker 完工后先落盘交付、再发消息叫醒监工；上下文约 6 万时 Codex 监工换班（新会话自建心跳、置顶、改标题，旧心跳删除、旧会话归档），Claude Code 监工清空自己（来源：user_confirmed）

## success_criteria

- 生产引擎下缺 delivery_evidence 字段 → acceptance_rejected 且保留 handle，修正重报后 accepted；身份/篡改类仍 fail-closed（来源：system_inferred）
- 干净消费项目中派发的 visible-sdd create 请求 prompt 含协议正文；现有 protocol 标识逐字不变，在途 run 不受影响（来源：user_confirmed）
- vibe 新增 skill 安装命令：支持 subdir、拒绝 ../绝对路径/符号链接、写入 .vibe/config.json 记录（含 subdir）并物化到 .vibe/proposals/skills/<名>/；scan 报 valid；不带 subdir 的旧行为不变（来源：user_confirmed）
- 在本仓库实际装上 pm-ai-shipping 的 3 个 skill（锁 8607e3b），scan 均 valid（来源：user_confirmed）
- visible-sdd-worker 协议含中文审查硬规则与严重度映射表；prd-guide §2 含 9 个方法论引用及使用规则（来源：user_confirmed）
- 监工地址登记在磁盘并可查询；worker 派发指令含完工自报与唤醒步骤；vibe 提供精简轮次输出；心跳文字降级为兜底；Claude Code 上实测一次 worker→监工唤醒（来源：user_confirmed）
- upgrade_project 死分支与 upgrade.py 删除后全量测试绿（来源：system_inferred）

## non_goals

- 不 fork/vendor pm-skills 文本进 vibe_guide/；不装其余 57 个产品方法论（来源：user_confirmed）
- shipping-artifacts 接验收侧（#92 阶段 3）延后（来源：user_confirmed）
- 不改用户 Codex 全局配置；不新增 AGENTS.md 分发块（来源：user_confirmed）
- Codex 上推送唤醒与换班的真实延迟/成本本轮不在本机验证，由用户在另一台机器实证（来源：user_confirmed）
- 不含 deploy、发布、打 tag（来源：system_inferred）

## code_evidence

- 交付门排序：vibe_guide/monitor.py _apply_event 的 evaluate_delivery_evidence 早于 visible-sdd validate-first（来源：system_inferred）
- 派发 prompt：vibe_guide/runners/provider_action.py create_request prompt 仅一句话（来源：system_inferred）
- skill 安装：vibe_guide/skills.py install_skill/_materialize_commit，无 CLI 调用方（来源：system_inferred）
- 死代码：vibe_guide/cli.py upgrade 分支与 vibe_guide/upgrade.py（来源：system_inferred）

## 已批准产品决策

- 本轮 PRD 覆盖哪些 issue → 6 个全做
- 外部方法论 skill 如何送到 agent 手里 → 新建通道：vibe 按锁定 SHA 拉取并复制进项目 .vibe/proposals/skills/<名>/，协议按路径引用
- #94 prd-guide §2 引用哪些 pm-skills 方法论 → 9 个：opportunity-solution-tree, north-star-metric, metrics-dashboard, prioritize-features, prioritization-frameworks, job-stories, customer-journey-map(可选), strategy-red-team, pre-mortem
- #93 visible-sdd 协议如何送达 worker → 派发时把协议全文放进 worker 指令，原标识仅作协议版本号
- #92 装哪些 skill、审查方法怎么送到 worker → 装 code-review/intended-vs-implemented/shipping-artifacts；硬规则中文写进 worker 协议随派发送达；完整 skill 可参考；不加第 5 个 AGENTS.md 块
- #91 监工形态 → 专职监工会话，每处理完即清空自己（地址不变）
- #91 Codex 上如何控制监工上下文 → 监工换班：超上限开接班会话、更新磁盘登记地址、归档自己

证据优先级：current_user > approved_prd > authorization > issue_contract > implementation
