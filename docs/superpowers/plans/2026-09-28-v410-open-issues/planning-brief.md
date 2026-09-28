# Planning Brief

## 产品目标
一次性关闭 GitHub 上 6 个开放 issue（#80 #88 #91 #92 #93 #94）：worker 格式错误可同会话重报；visible-sdd 协议在任何平台都送达 worker；vibe 能按需安装 monorepo 子目录 skill 并送进项目；reviewer 与 PRD 引导获得外部方法论；监工改为事件唤醒、上下文可控，停止空转烧 token；清理不可达的旧升级代码。（来源：user_confirmed）

## 用户场景
- worker 交付格式写错，收到可恢复拒绝后在同一会话修正重报，节点不被隔离（来源：system_inferred）
- 在任意消费项目、任意平台派发 visible-sdd worker，worker 指令里就带着协议全文（来源：user_confirmed）
- 产品经理需要某个方法论时，agent 按需执行一条安装命令，skill 按锁定版本落到项目 .vibe/proposals/skills/<名>/ 并被协议引用（来源：user_confirmed）
- PRD 讨论到成功标准时，agent 可参考 north-star-metric 等方法论，用中文推进，不另存 markdown，来源标记与停等门不变（来源：user_confirmed）
- 每次心跳先跑本地预检（没事/有活/该换班）；没事约 2 轮结束；worker 完工后先落盘交付、再发消息叫醒监工；上下文约 6 万时 Codex 监工换班（新会话自建心跳、置顶、改标题，旧心跳删除、旧会话归档），Claude Code 监工清空自己（来源：user_confirmed）

## 代码现状
- 交付门排序：vibe_guide/monitor.py _apply_event 的 evaluate_delivery_evidence 早于 visible-sdd validate-first（来源：system_inferred）
- 派发 prompt：vibe_guide/runners/provider_action.py create_request prompt 仅一句话（来源：system_inferred）
- skill 安装：vibe_guide/skills.py install_skill/_materialize_commit，无 CLI 调用方（来源：system_inferred）
- 死代码：vibe_guide/cli.py upgrade 分支与 vibe_guide/upgrade.py（来源：system_inferred）

## 方案
按已确认 PRD 目标生成最小变更方案。

## 非目标
- 不 fork/vendor pm-skills 文本进 vibe_guide/；不装其余 57 个产品方法论（来源：user_confirmed）
- shipping-artifacts 接验收侧（#92 阶段 3）延后（来源：user_confirmed）
- 不改用户 Codex 全局配置；不新增 AGENTS.md 分发块（来源：user_confirmed）
- Codex 上推送唤醒与换班的真实延迟/成本本轮不在本机验证，由用户在另一台机器实证（来源：user_confirmed）
- 不含 deploy、发布、打 tag（来源：system_inferred）

## Spec/Issue 映射与运行时验收
| Goal | User scenario | Current evidence | Spec | Issue | DAG node | Runtime acceptance |
|---|---|---|---|---|---|---|
| g-88 | worker 格式错误同会话重报 | vibe_guide/monitor.py _apply_event | success_criteria[0] | #88 | ['delivery-gate-rereport'] | 生产引擎 fixture 重报收口 |
| g-80 | 清理死代码 | vibe_guide/cli.py upgrade 分支 | success_criteria[6] | #80 | ['remove-dead-upgrade'] | vibe upgrade 输出不变 |
| g-93 | 协议送达任意平台 worker | vibe_guide/runners/provider_action.py create prompt | success_criteria[1] | #93 | ['inline-sdd-protocol'] | 临时消费项目派发请求含协议正文 |
| g-92 | 按需安装子目录 skill 并接入 reviewer | vibe_guide/skills.py install_skill | success_criteria[2..4] | #92 | ['skill-subdir-install', 'review-methodology'] | 本仓库实际安装 3 个 pm-ai-shipping skill 且 scan valid |
| g-94 | PRD 讨论可参考方法论 | vibe_guide/protocols/prd-guide.md §2 | success_criteria[4] | #94 | ['prd-guide-methodology'] | 协议引用表可解析到已安装 skill 路径 |
| g-91 | 事件唤醒、上下文可控的监工 | vibe_guide/supervisor.py; vibe_guide/runners/provider_action.py | success_criteria[5] | #91 | ['supervisor-registry', 'worker-push-delivery'] | Claude Code 实测一次唤醒；Codex 由用户异机实证 |
